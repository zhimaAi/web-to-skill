#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import html as html_escape
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urljoin, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fetch_rendered_html import DEFAULT_DNS_TIMEOUT_MS, URLSafetyError, URLSafetyGuard

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - optional dependency
    BeautifulSoup = None

try:
    from lxml import html as lxml_html
except Exception:  # pragma: no cover - optional fallback dependency
    lxml_html = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_OUT_DIR = "work/crawl"
DEFAULT_CONCURRENCY = 6
DEFAULT_MAX_PAGES = 10000
DEFAULT_TIMEOUT_MS = 120000
DEFAULT_WAIT_MS = 1000
DEFAULT_BODY_CHAR_LIMIT = 20000
DEFAULT_CHUNK_CHAR_LIMIT = 6000
MAX_HTTP_REDIRECTS = 10


@dataclass(frozen=True)
class DomainRule:
    domains: tuple[str, ...]
    anchor_selectors: tuple[str, ...]
    body_selector: str
    crawl_strategy: str = "directory"
    crawl_depth: int | None = None
    reason: str = ""
    anchor_scroll_selectors: tuple[str, ...] = ()
    link_token_attr: str = ""
    link_token_param: str = ""
    link_url_template: str = ""
    link_text_selector: str = ""


@dataclass(frozen=True)
class CrawlPlan:
    strategy: str
    depth: int
    decision_source: str
    reason: str
    needs_model_decision: bool = False


DOMAIN_RULES = (
    DomainRule(
        domains=("www.yuque.com",),
        anchor_selectors=(".ant-tabs-content-holder a",),
        anchor_scroll_selectors=(".ant-tabs-content-holder",),
        body_selector="article.article-content",
        crawl_depth=1,
        reason="Yuque exposes the book/catalog links in the tab content tree, so crawl the catalog links once.",
    ),
    DomainRule(
        domains=("my.feishu.cn",),
        anchor_selectors=('.wiki-tree-inner-container [data-node-uid*="wikiToken="]',),
        anchor_scroll_selectors=(".workspace-scroll-area",),
        body_selector=".docx-page-main, .wiki-doc-content, .suite-docx, main",
        crawl_depth=1,
        reason="Feishu wiki pages expose the knowledge-base tree in the sidebar, so crawl the tree links once.",
        link_token_attr="data-node-uid",
        link_token_param="wikiToken",
        link_url_template="https://my.feishu.cn/wiki/%s",
        link_text_selector=".workspace-tree-view-node-content",
    ),
    DomainRule(
        domains=("docs.openclaw.ai",),
        anchor_selectors=('aside.sidebar nav a[href^="/zh-CN/"], nav.tabs a[href^="/zh-CN/"]',),
        body_selector="article.article, main article",
        crawl_depth=2,
        reason="OpenClaw docs expose sidebar and tab navigation; depth 2 preserves existing section-to-section coverage.",
    ),
)

CRAWL_STRATEGIES = ("auto", "directory", "sequential-nav", "body-depth", "batch")

GENERIC_DIRECTORY_SELECTORS = (
    "nav a",
    "aside a",
    "[role='navigation'] a",
    "[class*='sidebar'] a",
    "[class*='sider'] a",
    "[class*='toc'] a",
    "[class*='catalog'] a",
    "[class*='directory'] a",
    "[class*='menu'] a",
    "[class*='tree'] a",
    "[class*='nav'] a",
)

BODY_CONTAINER_SELECTORS = (
    "article",
    "main",
    ".docx-page-main",
    ".wiki-doc-content",
    ".suite-docx",
)

SEQUENTIAL_LINK_RE = re.compile(
    r"("
    r"上一篇|下一篇|上一章|下一章|上一节|下一节|上一个|下一个|"
    r"前一篇|后一篇|前一章|后一章|"
    r"\bprev(?:ious)?\b|\bnext\b"
    r")",
    re.IGNORECASE,
)


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected integer, got {value!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("expected a non-negative integer")
    return parsed


def positive_int(value: str) -> int:
    parsed = nonnegative_int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return parsed


def depth_arg(value: str) -> int | str:
    if value.strip().lower() == "auto":
        return "auto"
    return nonnegative_int(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl web pages into an index with saved rendered HTML snapshots.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--start-url", help="Single start URL to crawl.")
    parser.add_argument(
        "--url-list",
        help="UTF-8 text file with one URL per line, or a JSON array of URL strings. Forces batch depth 0 crawling.",
    )
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="Output directory for index.json, raw text, chunks, and HTML.")
    parser.add_argument("--depth", default="auto", type=depth_arg, help="Crawl depth, or auto for known-domain rules/probe-only.")
    parser.add_argument("--crawl-strategy", choices=CRAWL_STRATEGIES, default="auto", help="Link discovery strategy.")
    parser.add_argument("--probe-only", action="store_true", help="Fetch only the first page and write probe evidence.")
    parser.add_argument("--concurrency", default=DEFAULT_CONCURRENCY, type=positive_int, help="Concurrent page fetches.")
    parser.add_argument("--max-pages", default=DEFAULT_MAX_PAGES, type=nonnegative_int, help="Safety cap for scheduled pages; 0 disables the cap.")
    parser.add_argument("--browser", default="playwright", choices=("http", "playwright"), help="Fetching backend.")
    parser.add_argument("--link-scope", default="same-host", choices=("same-host", "same-origin"), help="Scope filter for discovered links.")
    parser.add_argument("--timeout-ms", default=DEFAULT_TIMEOUT_MS, type=positive_int, help="Per-page navigation/fetch timeout.")
    parser.add_argument("--wait-ms", default=DEFAULT_WAIT_MS, type=nonnegative_int, help="Extra wait after Playwright page load.")
    parser.add_argument("--dns-timeout-ms", default=DEFAULT_DNS_TIMEOUT_MS, type=positive_int, help=argparse.SUPPRESS)
    return parser.parse_args()


def crawl_output_paths(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    out_dir = Path(args.out_dir)
    return out_dir, out_dir / "index.json", out_dir / "raw", out_dir / "html"


def normalize_link(raw_href: str, base_url: str) -> str:
    raw_href = raw_href.strip()
    if not raw_href:
        raise ValueError("empty href")
    lowered = raw_href.lower()
    for prefix in ("javascript:", "mailto:", "tel:", "data:", "sms:", "weixin:"):
        if lowered.startswith(prefix):
            raise ValueError(f"unsupported href scheme: {prefix}")
    if raw_href.startswith("#"):
        raise ValueError("fragment-only href")

    absolute = urlparse(urljoin(base_url, raw_href))
    if absolute.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {absolute.scheme}")
    host = (absolute.hostname or "").lower()
    netloc = host
    if absolute.port and not (
        (absolute.scheme == "http" and absolute.port == 80)
        or (absolute.scheme == "https" and absolute.port == 443)
    ):
        netloc = f"{host}:{absolute.port}"
    path = absolute.path or "/"
    return urlunparse((absolute.scheme, netloc, path, "", absolute.query, ""))


def rule_for_url(raw_url: str) -> DomainRule | None:
    host = (urlparse(raw_url).hostname or "").lower()
    for rule in DOMAIN_RULES:
        for domain in rule.domains:
            domain_host = (urlparse("https://" + domain if "://" not in domain else domain).hostname or "").lower()
            if host == domain_host or host.endswith("." + domain_host):
                return rule
    return None


def domain_rule_payload(rule: DomainRule | None) -> dict[str, Any]:
    if not rule:
        return {"matched": False}
    return {
        "matched": True,
        "domains": list(rule.domains),
        "crawl_strategy": rule.crawl_strategy,
        "crawl_depth": rule.crawl_depth,
        "reason": rule.reason,
    }


def read_url_list(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8-sig")
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        payload = json.loads(stripped)
        if not isinstance(payload, list):
            raise ValueError("--url-list JSON must be an array of URL strings")
        urls = []
        for item in payload:
            if not isinstance(item, str):
                raise ValueError("--url-list JSON must contain only URL strings")
            urls.append(item)
        return urls
    urls = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


def load_start_urls(args: argparse.Namespace) -> list[str]:
    raw_urls: list[str] = []
    if args.start_url:
        raw_urls.append(args.start_url)
    if args.url_list:
        raw_urls.extend(read_url_list(Path(args.url_list)))
    if not raw_urls:
        raise ValueError("provide --start-url or --url-list")

    seen: set[str] = set()
    urls: list[str] = []
    for raw_url in raw_urls:
        normalized = normalize_link(raw_url, raw_url)
        if normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)
    if not urls:
        raise ValueError("no usable URLs found")
    return urls


def resolve_crawl_plan(args: argparse.Namespace, start_url: str) -> CrawlPlan:
    rule = rule_for_url(start_url)
    raw_strategy = args.crawl_strategy
    needs_model_decision = False

    if args.url_list or raw_strategy == "batch":
        return CrawlPlan(
            strategy="batch",
            depth=0,
            decision_source="url_list",
            reason="Batch URL mode fetches the provided URLs directly and does not discover links.",
            needs_model_decision=False,
        )

    if raw_strategy == "auto" and rule is not None:
        strategy = rule.crawl_strategy
        decision_source = "domain_rule"
        reason = rule.reason or "Matched a site-specific rule."
    elif raw_strategy == "auto":
        strategy = "body-depth"
        decision_source = "model_required"
        reason = (
            "No domain rule matched. Generate a probe first, then have the model choose "
            "directory, sequential-nav, or body-depth."
        )
        needs_model_decision = True
    else:
        strategy = raw_strategy
        decision_source = "user_or_model"
        reason = "Strategy was supplied explicitly."

    if args.depth == "auto":
        if rule is not None and rule.crawl_depth is not None:
            depth = rule.crawl_depth
        elif args.probe_only:
            depth = 0
        else:
            raise ValueError(
                "--depth auto only resolves automatically for known domains. "
                "For unknown domains, run --probe-only first and pass the model-selected numeric depth."
            )
    else:
        depth = int(args.depth)

    return CrawlPlan(
        strategy=strategy,
        depth=depth,
        decision_source=decision_source,
        reason=reason,
        needs_model_decision=needs_model_decision,
    )


def allowed_by_scope(candidate: str, start_url: str, scope: str) -> bool:
    cand = urlparse(candidate)
    start = urlparse(start_url)
    if scope == "same-host":
        return (cand.hostname or "").lower() == (start.hostname or "").lower()
    return (
        cand.scheme == start.scheme
        and (cand.hostname or "").lower() == (start.hostname or "").lower()
        and (cand.port or default_port(cand.scheme)) == (start.port or default_port(start.scheme))
    )


def default_port(scheme: str) -> int | None:
    if scheme == "http":
        return 80
    if scheme == "https":
        return 443
    return None


def collapse_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def clean_body_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [collapse_space(line) for line in value.split("\n")]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def split_keywords(raw: str) -> list[str]:
    if not raw.strip():
        return []
    seen: set[str] = set()
    result: list[str] = []
    for part in re.split(r"[,\uFF0C;\uFF1B\s]+", raw):
        part = part.strip()
        key = part.lower()
        if part and key not in seen:
            seen.add(key)
            result.append(part)
    return result


def first_nonempty(*values: str) -> str:
    for value in values:
        value = collapse_space(value)
        if value:
            return value
    return ""


def relpath(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return Path(Path.cwd(), path).resolve().as_posix()


def page_id_for(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def split_chunks(text: str, limit: int) -> list[str]:
    if not text:
        return []
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in re.split(r"\n{2,}", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) > limit:
            if current:
                chunks.append("\n\n".join(current))
                current = []
                current_len = 0
            for start in range(0, len(paragraph), limit):
                chunks.append(paragraph[start : start + limit])
            continue
        next_len = current_len + len(paragraph) + (2 if current else 0)
        if current and next_len > limit:
            chunks.append("\n\n".join(current))
            current = [paragraph]
            current_len = len(paragraph)
        else:
            current.append(paragraph)
            current_len = next_len
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def write_content_files(
    page_url: str,
    html_text: str,
    body_text: str,
    raw_dir: Path,
    html_dir: Path,
    index_base: Path,
    body_char_limit: int,
    chunk_char_limit: int,
) -> tuple[str, str, int, int, bool, list[dict[str, Any]]]:
    original_chars = len(body_text)
    truncated = False
    if body_char_limit and len(body_text) > body_char_limit:
        body_text = body_text[:body_char_limit]
        truncated = True

    raw_dir.mkdir(parents=True, exist_ok=True)
    html_dir.mkdir(parents=True, exist_ok=True)
    chunk_dir = raw_dir / "chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)

    page_id = page_id_for(page_url)
    html_file = html_dir / f"{page_id}.html"
    html_file.write_text(html_text, encoding="utf-8")

    content_file = raw_dir / f"{page_id}.txt"
    content_file.write_text(f"URL: {page_url}\n\n{body_text}\n", encoding="utf-8")

    chunks: list[dict[str, Any]] = []
    for index, chunk in enumerate(split_chunks(body_text, chunk_char_limit), start=1):
        chunk_file = chunk_dir / f"{page_id}-{index:03d}.txt"
        chunk_file.write_text(chunk + "\n", encoding="utf-8")
        chunks.append(
            {
                "path": relpath(chunk_file, index_base),
                "chars": len(chunk),
                "chunk_index": index,
            }
        )

    return relpath(html_file, index_base), relpath(content_file, index_base), original_chars, len(body_text), truncated, chunks


def append_text_section(snapshot: Any, parent: Any, body_text: str) -> None:
    section = snapshot.new_tag("section", attrs={"data-rendered-text": "true"})
    for paragraph in [item.strip() for item in body_text.splitlines() if item.strip()]:
        node = snapshot.new_tag("p")
        node.string = paragraph
        section.append(node)
    if section.contents:
        parent.append(section)


def build_snapshot_from_soup(
    page_url: str,
    title: str,
    description: str,
    keywords: list[str],
    body_nodes: list[Any],
    body_text: str,
) -> str:
    snapshot = BeautifulSoup("<!DOCTYPE html><html><head></head><body></body></html>", "html.parser")
    head = snapshot.head
    body = snapshot.body
    if head is None or body is None:
        return "<!DOCTYPE html>\n<html><body></body></html>\n"

    head.append(snapshot.new_tag("meta", attrs={"charset": "utf-8"}))
    head.append(snapshot.new_tag("meta", attrs={"name": "source-url", "content": page_url}))
    title_node = snapshot.new_tag("title")
    title_node.string = title
    head.append(title_node)
    if description:
        head.append(snapshot.new_tag("meta", attrs={"name": "description", "content": description}))
    if keywords:
        head.append(snapshot.new_tag("meta", attrs={"name": "keywords", "content": ", ".join(keywords)}))

    body["data-source-url"] = page_url
    main = snapshot.new_tag("main", attrs={"data-rendered-snapshot": "true"})
    if body_text:
        append_text_section(snapshot, main, body_text)

    rendered_html = snapshot.new_tag("section", attrs={"data-rendered-html": "true"})
    for node in body_nodes:
        if getattr(node, "name", "") == "body":
            html_fragment = "".join(str(child) for child in node.contents)
        else:
            html_fragment = str(node)
        fragment = BeautifulSoup(html_fragment, "html.parser")
        for child in list(fragment.contents):
            rendered_html.append(child)
    if rendered_html.contents:
        main.append(rendered_html)

    body.append(main)
    return snapshot.prettify(formatter="minimal") + "\n"


def build_snapshot_from_text(page_url: str, title: str, description: str, keywords: list[str], body_text: str) -> str:
    escaped_title = html_escape.escape(title)
    escaped_description = html_escape.escape(description, quote=True)
    escaped_keywords = html_escape.escape(", ".join(keywords), quote=True)
    escaped_url = html_escape.escape(page_url, quote=True)
    paragraphs = "\n".join(
        f"<p>{html_escape.escape(paragraph)}</p>"
        for paragraph in [item.strip() for item in body_text.splitlines() if item.strip()]
    )
    meta_description = f'<meta name="description" content="{escaped_description}">\n' if description else ""
    meta_keywords = f'<meta name="keywords" content="{escaped_keywords}">\n' if keywords else ""
    return (
        "<!DOCTYPE html>\n"
        "<html><head>\n"
        '<meta charset="utf-8">\n'
        f'<meta name="source-url" content="{escaped_url}">\n'
        f"<title>{escaped_title}</title>\n"
        f"{meta_description}{meta_keywords}"
        "</head>\n"
        f'<body data-source-url="{escaped_url}"><main data-rendered-snapshot="true">\n'
        f'<section data-rendered-text="true">\n{paragraphs}\n</section>\n'
        "</main></body></html>\n"
    )


def remove_unwanted_soup_nodes(soup: Any) -> None:
    for node in soup.select("script, style, noscript, template, link[rel='stylesheet']"):
        node.decompose()
    for node in soup.find_all(True):
        attrs = {}
        for key, value in node.attrs.items():
            lowered = key.lower().strip()
            if lowered == "style" or lowered.startswith("on"):
                continue
            attrs[key] = value
        node.attrs = attrs


def meta_content_soup(soup: Any, attr_name: str, attr_value: str) -> str:
    for meta in soup.find_all("meta"):
        if collapse_space(str(meta.get(attr_name, ""))).lower() == attr_value.lower():
            return collapse_space(str(meta.get("content", "")))
    return ""


def token_url(element: Any, rule: DomainRule) -> str:
    if not rule.link_token_attr or not rule.link_url_template:
        return ""
    attr_value = element.get(rule.link_token_attr) or ""
    if not attr_value:
        return ""
    token = attr_value
    if rule.link_token_param:
        token = parse_qs(attr_value).get(rule.link_token_param, [""])[0]
    if not token:
        return ""
    return rule.link_url_template.replace("%s", quote(token, safe=""))


def raw_href_for_soup(element: Any, rule: DomainRule | None) -> str:
    if rule:
        generated = token_url(element, rule)
        if generated:
            return generated
    if getattr(element, "name", "") == "a":
        return str(element.get("href") or "")
    nested = element.find("a") if hasattr(element, "find") else None
    if nested:
        return str(nested.get("href") or "")
    return ""


def text_for_soup(element: Any, rule: DomainRule | None) -> str:
    if rule and rule.link_text_selector:
        selected = element.select_one(rule.link_text_selector)
        if selected:
            return collapse_space(selected.get_text(" ", strip=True))
    return collapse_space(element.get_text(" ", strip=True))


def attr_text(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return " ".join(str(item) for item in value)
    return str(value or "")


def with_link_source(link: dict[str, str], source: str) -> dict[str, str]:
    copied = dict(link)
    copied["source"] = copied.get("source") or source
    return copied


def extract_links_from_soup_elements(
    elements: list[Any],
    page_url: str,
    source: str,
    rule: DomainRule | None = None,
) -> list[dict[str, str]]:
    seen: set[str] = set()
    links: list[dict[str, str]] = []
    for element in elements:
        if getattr(element, "name", "") == "a" or (rule and rule.link_token_attr and element.get(rule.link_token_attr)):
            targets = [element]
        else:
            targets = list(element.find_all("a")) if hasattr(element, "find_all") else []
        for target in targets:
            raw_href = raw_href_for_soup(target, rule)
            try:
                normalized = normalize_link(raw_href, page_url)
            except ValueError:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            links.append(
                {
                    "url": normalized,
                    "raw_href": raw_href.strip(),
                    "text": text_for_soup(target, rule),
                    "source": source,
                }
            )
    return links


def extract_links_by_selectors_soup(
    soup: Any,
    selectors: tuple[str, ...],
    page_url: str,
    source: str,
    rule: DomainRule | None = None,
) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for selector in selectors:
        try:
            elements = list(soup.select(selector))
        except Exception as exc:
            print(f"skip invalid selector {selector!r}: {exc}", file=sys.stderr)
            continue
        links = merge_links(links, extract_links_from_soup_elements(elements, page_url, source, rule))
    return links


def extract_links_soup(soup: Any, page_url: str, rule: DomainRule | None) -> list[dict[str, str]]:
    selectors = rule.anchor_selectors if rule and rule.anchor_selectors else ("a",)
    return extract_links_by_selectors_soup(soup, selectors, page_url, "all", rule)


def sequential_signal_soup(element: Any) -> str:
    attrs = []
    for key in ("rel", "aria-label", "title", "class", "id"):
        attrs.append(attr_text(element.get(key)))
    parent = element.parent if getattr(element, "parent", None) else None
    if parent is not None:
        attrs.extend(attr_text(parent.get(key)) for key in ("aria-label", "title", "class", "id"))
    return collapse_space(" ".join([text_for_soup(element, None), *attrs]))


def extract_sequential_links_soup(soup: Any, page_url: str) -> list[dict[str, str]]:
    elements = []
    for element in soup.find_all("a"):
        if SEQUENTIAL_LINK_RE.search(sequential_signal_soup(element)):
            elements.append(element)
    return extract_links_from_soup_elements(elements, page_url, "sequential_nav", None)


def body_link_nodes_soup(soup: Any, body_nodes: list[Any], rule: DomainRule | None) -> list[Any]:
    if rule is not None:
        return body_nodes
    nodes: list[Any] = []
    for selector in BODY_CONTAINER_SELECTORS:
        try:
            nodes.extend(soup.select(selector))
        except Exception:
            continue
    return nodes or body_nodes


def classify_links_soup(
    soup: Any,
    page_url: str,
    rule: DomainRule | None,
    body_nodes: list[Any],
    browser_links: list[dict[str, str]],
) -> dict[str, list[dict[str, str]]]:
    directory_links: list[dict[str, str]] = []
    if rule and rule.anchor_selectors:
        directory_links = extract_links_by_selectors_soup(soup, rule.anchor_selectors, page_url, "directory", rule)
        directory_links = merge_links(directory_links, [with_link_source(link, "directory") for link in browser_links])
    else:
        directory_links = extract_links_by_selectors_soup(
            soup, GENERIC_DIRECTORY_SELECTORS, page_url, "directory", None
        )

    sequential_links = extract_sequential_links_soup(soup, page_url)
    body_links = extract_links_from_soup_elements(body_link_nodes_soup(soup, body_nodes, rule), page_url, "body", None)
    all_links = merge_links(directory_links, merge_links(sequential_links, body_links))
    all_links = merge_links(all_links, extract_links_by_selectors_soup(soup, ("a",), page_url, "all", None))

    return {
        "directory": directory_links,
        "sequential_nav": sequential_links,
        "body": body_links,
        "all": all_links,
    }


def links_for_strategy(candidates: dict[str, list[dict[str, str]]], strategy: str) -> list[dict[str, str]]:
    if strategy == "directory":
        return candidates.get("directory", [])
    if strategy == "sequential-nav":
        return candidates.get("sequential_nav", [])
    if strategy == "body-depth":
        return candidates.get("body", [])
    return candidates.get("all", [])


def compact_link_evidence(
    candidates: dict[str, list[dict[str, str]]],
    sample_limit: int = 30,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for key in ("directory", "sequential_nav", "body", "all"):
        links = candidates.get(key, [])
        evidence[key] = {
            "count": len(links),
            "samples": [
                {
                    "url": link.get("url", ""),
                    "text": link.get("text", ""),
                    "raw_href": link.get("raw_href", ""),
                }
                for link in links[:sample_limit]
            ],
        }
    return evidence


def parse_with_soup(
    html_text: str,
    page_url: str,
    depth: int,
    need_links: bool,
    crawl_strategy: str,
    browser_links: list[dict[str, str]],
    browser_body_text: str,
    raw_dir: Path,
    html_dir: Path,
    index_base: Path,
    body_char_limit: int,
    chunk_char_limit: int,
) -> dict[str, Any]:
    rule = rule_for_url(page_url)
    soup = BeautifulSoup(html_text, "html.parser")
    remove_unwanted_soup_nodes(soup)
    head = soup.head or soup

    title = first_nonempty(
        meta_content_soup(head, "property", "og:title"),
        meta_content_soup(head, "name", "twitter:title"),
        head.title.get_text(" ", strip=True) if head.title else "",
    )
    description = first_nonempty(
        meta_content_soup(head, "name", "description"),
        meta_content_soup(head, "property", "og:description"),
        meta_content_soup(head, "name", "twitter:description"),
    )
    keywords = split_keywords(meta_content_soup(head, "name", "keywords"))

    body_nodes = soup.select(rule.body_selector) if rule and rule.body_selector else []
    matched_rule_body = bool(body_nodes)
    if not body_nodes:
        body_nodes = [soup.body or soup]
    body_text = clean_body_text("\n\n".join(node.get_text("\n", strip=True) for node in body_nodes))
    rendered_text = clean_body_text(browser_body_text)
    if len(rendered_text) > len(body_text) and (not matched_rule_body or not body_text):
        body_text = rendered_text
    snapshot_html = build_snapshot_from_soup(page_url, title, description, keywords, body_nodes, body_text)

    link_candidates = classify_links_soup(soup, page_url, rule, body_nodes, browser_links) if need_links else {}
    links = links_for_strategy(link_candidates, crawl_strategy) if need_links else []
    html_path, content_path, content_chars, saved_chars, truncated, chunks = write_content_files(
        page_url, snapshot_html, body_text, raw_dir, html_dir, index_base, body_char_limit, chunk_char_limit
    )
    return {
        "url": page_url,
        "final_url": page_url,
        "depth": depth,
        "title": title,
        "description": description,
        "keywords": keywords,
        "html_path": html_path,
        "content_path": content_path,
        "content_chars": content_chars,
        "saved_content_chars": saved_chars,
        "content_truncated": truncated,
        "chunks": chunks,
        "links": links,
        "link_strategy": crawl_strategy,
        "link_evidence": compact_link_evidence(link_candidates) if need_links else {},
    }


def parse_with_lxml(
    html_text: str,
    page_url: str,
    depth: int,
    need_links: bool,
    crawl_strategy: str,
    browser_links: list[dict[str, str]],
    browser_body_text: str,
    raw_dir: Path,
    html_dir: Path,
    index_base: Path,
    body_char_limit: int,
    chunk_char_limit: int,
) -> dict[str, Any]:
    if lxml_html is None:
        raise RuntimeError("Install beautifulsoup4 or lxml to parse HTML.")
    doc = lxml_html.fromstring(html_text)
    title = first_nonempty(
        " ".join(doc.xpath("//head/meta[translate(@property,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='og:title']/@content")),
        " ".join(doc.xpath("//head/meta[translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='twitter:title']/@content")),
        " ".join(doc.xpath("//head/title/text()")),
    )
    description = first_nonempty(
        " ".join(doc.xpath("//head/meta[translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='description']/@content")),
        " ".join(doc.xpath("//head/meta[translate(@property,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='og:description']/@content")),
        " ".join(doc.xpath("//head/meta[translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='twitter:description']/@content")),
    )
    keywords = split_keywords(
        " ".join(doc.xpath("//head/meta[translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='keywords']/@content"))
    )
    body_nodes = doc.xpath("//body")
    body_text = clean_body_text("\n".join(node.text_content() for node in (body_nodes or [doc])))
    rendered_text = clean_body_text(browser_body_text)
    if len(rendered_text) > len(body_text):
        body_text = rendered_text
    snapshot_html = build_snapshot_from_text(page_url, title, description, keywords, body_text)

    link_candidates: dict[str, list[dict[str, str]]] = {}
    if need_links:
        def read_lxml_links(nodes: list[Any], source: str) -> list[dict[str, str]]:
            seen: set[str] = set()
            result: list[dict[str, str]] = []
            for node in nodes:
                raw_href = node.get("href", "")
                try:
                    normalized = normalize_link(raw_href, page_url)
                except ValueError:
                    continue
                if normalized in seen:
                    continue
                seen.add(normalized)
                result.append(
                    {
                        "url": normalized,
                        "raw_href": raw_href.strip(),
                        "text": collapse_space(node.text_content()),
                        "source": source,
                    }
                )
            return result

        directory_nodes = doc.xpath("//nav//a[@href] | //aside//a[@href] | //*[@role='navigation']//a[@href]")
        sequential_nodes = []
        for node in doc.xpath("//a[@href]"):
            signal = collapse_space(
                " ".join(
                    [
                        node.text_content(),
                        attr_text(node.get("rel")),
                        attr_text(node.get("aria-label")),
                        attr_text(node.get("title")),
                        attr_text(node.get("class")),
                        attr_text(node.get("id")),
                    ]
                )
            )
            if SEQUENTIAL_LINK_RE.search(signal):
                sequential_nodes.append(node)
        body_link_nodes = doc.xpath("//article//a[@href] | //main//a[@href]") or doc.xpath("//body//a[@href]")
        directory_links = merge_links(
            read_lxml_links(directory_nodes, "directory"),
            [with_link_source(link, "directory") for link in browser_links],
        )
        sequential_links = read_lxml_links(sequential_nodes, "sequential_nav")
        body_links = read_lxml_links(body_link_nodes, "body")
        all_links = merge_links(directory_links, merge_links(sequential_links, body_links))
        all_links = merge_links(all_links, read_lxml_links(doc.xpath("//a[@href]"), "all"))
        link_candidates = {
            "directory": directory_links,
            "sequential_nav": sequential_links,
            "body": body_links,
            "all": all_links,
        }
    links = links_for_strategy(link_candidates, crawl_strategy) if need_links else []
    html_path, content_path, content_chars, saved_chars, truncated, chunks = write_content_files(
        page_url, snapshot_html, body_text, raw_dir, html_dir, index_base, body_char_limit, chunk_char_limit
    )
    return {
        "url": page_url,
        "final_url": page_url,
        "depth": depth,
        "title": title,
        "description": description,
        "keywords": keywords,
        "html_path": html_path,
        "content_path": content_path,
        "content_chars": content_chars,
        "saved_content_chars": saved_chars,
        "content_truncated": truncated,
        "chunks": chunks,
        "links": links,
        "link_strategy": crawl_strategy,
        "link_evidence": compact_link_evidence(link_candidates) if need_links else {},
    }


def parse_page(
    html_text: str,
    page_url: str,
    depth: int,
    need_links: bool,
    crawl_strategy: str,
    browser_links: list[dict[str, str]],
    browser_body_text: str,
    raw_dir: Path,
    html_dir: Path,
    index_base: Path,
    body_char_limit: int,
    chunk_char_limit: int,
) -> dict[str, Any]:
    if BeautifulSoup is not None:
        return parse_with_soup(
            html_text,
            page_url,
            depth,
            need_links,
            crawl_strategy,
            browser_links,
            browser_body_text,
            raw_dir,
            html_dir,
            index_base,
            body_char_limit,
            chunk_char_limit,
        )
    return parse_with_lxml(
        html_text,
        page_url,
        depth,
        need_links,
        crawl_strategy,
        browser_links,
        browser_body_text,
        raw_dir,
        html_dir,
        index_base,
        body_char_limit,
        chunk_char_limit,
    )


def merge_links(left: list[dict[str, str]], right: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    merged: list[dict[str, str]] = []
    for group in (left, right):
        for link in group:
            url = link.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(link)
    return merged


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def fetch_http_once_blocking(url: str, timeout_ms: int) -> tuple[int, str, dict[str, str], bytes, str]:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    opener = build_opener(NoRedirectHandler())
    try:
        with opener.open(req, timeout=timeout_ms / 1000) as response:
            status = response.getcode() or 200
            charset = response.headers.get_content_charset() or "utf-8"
            data = response.read()
            return status, response.geturl(), dict(response.headers.items()), data, charset
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            return exc.code, exc.geturl(), dict(exc.headers.items()), b"", "utf-8"
        raise RuntimeError(f"http status {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc


def redirect_location(headers: dict[str, str]) -> str:
    for key, value in headers.items():
        if key.lower() == "location":
            return value.strip()
    return ""


BROWSER_LINK_SCRIPT = r"""
async ({ itemSelector, scrollSelector, linkTokenAttr, linkTokenParam, linkURLTemplate, linkTextSelector }) => {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const seen = new Map();
  const readAttr = (el, name) => (!el || !name) ? "" : (el.getAttribute(name) || "");
  const tokenURL = el => {
    if (!linkTokenAttr || !linkURLTemplate) return "";
    const attrValue = readAttr(el, linkTokenAttr);
    if (!attrValue) return "";
    let token = attrValue;
    if (linkTokenParam) {
      const params = new URLSearchParams(attrValue);
      token = params.get(linkTokenParam) || "";
    }
    if (!token) return "";
    return linkURLTemplate.replace("%s", encodeURIComponent(token));
  };
  const rawHrefFor = el => {
    const generatedURL = tokenURL(el);
    if (generatedURL) return generatedURL;
    if (el.matches("a")) return el.getAttribute("href") || el.href || "";
    const a = el.querySelector("a");
    return a ? (a.getAttribute("href") || a.href || "") : "";
  };
  const textFor = el => {
    const textEl = linkTextSelector ? el.querySelector(linkTextSelector) : null;
    const source = textEl || el;
    return (source.innerText || source.textContent || "").trim();
  };
  const keyFor = el => rawHrefFor(el);
  const addItems = () => {
    for (const el of Array.from(document.querySelectorAll(itemSelector))) {
      const rawHref = rawHrefFor(el);
      const text = textFor(el);
      if (rawHref && !seen.has(rawHref)) seen.set(rawHref, text);
    }
  };
  const expandVisible = root => {
    let clicked = 0;
    const iconSelector = [
      '[class*="collapseIconWrapper"][class*="collapsed"]',
      '[class*="workspace-tree-view-node-expand-arrow--collapsed"]'
    ].join(",");
    for (const icon of Array.from(root.querySelectorAll(iconSelector))) {
      const before = Array.from(document.querySelectorAll(itemSelector)).map(keyFor).join("|");
      const link = icon.closest("a");
      const href = link ? link.getAttribute("href") : null;
      if (link) link.removeAttribute("href");
      icon.click();
      if (link && href !== null) link.setAttribute("href", href);
      clicked++;
      const after = Array.from(document.querySelectorAll(itemSelector)).map(keyFor).join("|");
      if (before === after) {
        const wrapper = icon.closest('[class*="collapseIconWrapper"], [class*="workspace-tree-view-node-expand-arrow"]');
        if (wrapper && wrapper !== icon) wrapper.click();
      }
    }
    return clicked;
  };
  addItems();
  const roots = Array.from(document.querySelectorAll(scrollSelector || "body"));
  for (const root of roots) {
    const candidates = [root, ...Array.from(root.querySelectorAll("*"))]
      .filter(el => el.scrollHeight > el.clientHeight + 20);
    candidates.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
    const scroller = candidates[0] || root;
    let lastSeenSize = -1;
    let lastScrollHeight = -1;
    for (let pass = 0; pass < 10; pass++) {
      let clicked = 0;
      const step = Math.max(80, Math.floor((scroller.clientHeight || 500) * 0.75));
      const maxScroll = scroller.scrollHeight + scroller.clientHeight;
      for (let y = 0; y <= maxScroll; y += step) {
        scroller.scrollTop = y;
        scroller.dispatchEvent(new Event("scroll", { bubbles: true }));
        await sleep(120);
        addItems();
        clicked += expandVisible(root);
        if (clicked > 0) {
          await sleep(160);
          addItems();
        }
      }
      scroller.scrollTop = scroller.scrollHeight;
      scroller.dispatchEvent(new Event("scroll", { bubbles: true }));
      await sleep(250);
      addItems();
      clicked += expandVisible(root);
      if (clicked === 0 && seen.size === lastSeenSize && scroller.scrollHeight === lastScrollHeight) break;
      lastSeenSize = seen.size;
      lastScrollHeight = scroller.scrollHeight;
    }
  }
  return Array.from(seen.entries()).map(([raw_href, text]) => ({ raw_href, text }));
}
"""


class Crawler:
    def __init__(
        self,
        args: argparse.Namespace,
        raw_dir: Path,
        html_dir: Path,
        index_base: Path,
        plan: CrawlPlan,
    ) -> None:
        self.args = args
        self.raw_dir = raw_dir
        self.html_dir = html_dir
        self.index_base = index_base
        self.plan = plan
        self.visited: set[str] = set()
        self.max_pages_truncated = False
        self.sem = asyncio.Semaphore(args.concurrency)
        self.url_guard = URLSafetyGuard(args.dns_timeout_ms)
        self.playwright = None
        self.browser = None
        self.context = None

    async def __aenter__(self) -> "Crawler":
        if self.args.browser == "playwright":
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise RuntimeError(
                    "Playwright mode requires: python -m pip install playwright beautifulsoup4 lxml; "
                    "python -m playwright install chromium"
                ) from exc
            self.playwright = await async_playwright().start()
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ]
            if os.environ.get("PLAYWRIGHT_NO_SANDBOX", "1").lower() not in {"0", "false", "no"}:
                launch_args.extend(["--no-sandbox", "--disable-setuid-sandbox"])
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=launch_args,
            )
            self.context = await self.browser.new_context(
                ignore_https_errors=True,
                locale="zh-CN",
                user_agent=USER_AGENT,
                viewport={"width": 1440, "height": 1000},
            )
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.context is not None:
            await self.context.close()
        if self.browser is not None:
            await self.browser.close()
        if self.playwright is not None:
            await self.playwright.stop()

    async def crawl(self, start_url: str) -> list[dict[str, Any]]:
        queue: list[tuple[str, int]] = [(start_url, 0)]
        self.visited.add(start_url)
        results: list[dict[str, Any]] = []

        while queue:
            batch = queue
            queue = []
            fetched_pages = await asyncio.gather(
                *(self.fetch_item(url, depth, depth < self.plan.depth) for url, depth in batch)
            )
            for item_url, item_depth, page_result in fetched_pages:
                results.append(page_result)
                if page_result.get("error") or item_depth >= self.plan.depth:
                    continue
                for link in page_result.get("links", []):
                    link_url = link.get("url", "")
                    if not link_url or link_url in self.visited:
                        continue
                    if not allowed_by_scope(link_url, start_url, self.args.link_scope):
                        continue
                    if self.args.max_pages and len(self.visited) >= self.args.max_pages:
                        self.max_pages_truncated = True
                        break
                    self.visited.add(link_url)
                    queue.append((link_url, item_depth + 1))
        return results

    async def crawl_batch(self, urls: list[str]) -> list[dict[str, Any]]:
        selected: list[str] = []
        for url in urls:
            if url in self.visited:
                continue
            if self.args.max_pages and len(selected) >= self.args.max_pages:
                self.max_pages_truncated = True
                break
            self.visited.add(url)
            selected.append(url)
        fetched_pages = await asyncio.gather(*(self.fetch_item(url, 0, False) for url in selected))
        return [page_result for _, _, page_result in fetched_pages]

    async def probe(self, start_url: str) -> dict[str, Any]:
        _, _, page_result = await self.fetch_item(start_url, 0, True)
        return page_result

    async def fetch_item(self, url: str, depth: int, need_links: bool) -> tuple[str, int, dict[str, Any]]:
        async with self.sem:
            try:
                if self.args.browser == "playwright":
                    result = await self.fetch_playwright(url, depth, need_links)
                else:
                    result = await self.fetch_http(url, depth, need_links)
            except Exception as exc:
                result = {"url": url, "depth": depth, "error": str(exc)}
            return url, depth, result

    async def fetch_http(self, url: str, depth: int, need_links: bool) -> dict[str, Any]:
        current_url = url
        for redirect_count in range(MAX_HTTP_REDIRECTS + 1):
            await self.url_guard.validate(current_url)
            status, response_url, headers, data, charset = await asyncio.to_thread(
                fetch_http_once_blocking, current_url, self.args.timeout_ms
            )
            if 300 <= status < 400:
                location = redirect_location(headers)
                if not location:
                    raise RuntimeError(f"redirect response {status} has no Location header")
                if redirect_count >= MAX_HTTP_REDIRECTS:
                    raise RuntimeError(f"too many redirects (>{MAX_HTTP_REDIRECTS})")
                current_url = normalize_link(location, current_url)
                continue
            final_url = normalize_link(response_url or current_url, current_url)
            await self.url_guard.validate(final_url)
            html_text = data.decode(charset, errors="replace")
            break
        else:  # pragma: no cover - the loop raises at the configured limit
            raise RuntimeError(f"too many redirects (>{MAX_HTTP_REDIRECTS})")
        return parse_page(
            html_text,
            final_url,
            depth,
            need_links,
            self.plan.strategy,
            [],
            "",
            self.raw_dir,
            self.html_dir,
            self.index_base,
            self.args.body_char_limit,
            self.args.chunk_char_limit,
        )

    async def fetch_playwright(self, url: str, depth: int, need_links: bool) -> dict[str, Any]:
        await self.url_guard.validate(url)
        page = await self.context.new_page()
        blocked_navigation_error: URLSafetyError | None = None

        async def validate_route(route: Any, request: Any) -> None:
            nonlocal blocked_navigation_error
            request_url = request.url
            if not request_url.startswith(("http://", "https://")):
                if request.is_navigation_request():
                    blocked_navigation_error = URLSafetyError(f"blocked navigation scheme: {urlparse(request_url).scheme}")
                await route.abort("blockedbyclient")
                return
            try:
                await self.url_guard.validate(request_url)
            except URLSafetyError as exc:
                if request.is_navigation_request():
                    blocked_navigation_error = exc
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        await page.route("**/*", validate_route)
        try:
            try:
                response = await page.goto(
                    url,
                    timeout=self.args.timeout_ms,
                    wait_until="domcontentloaded",
                )
            except Exception as exc:
                if blocked_navigation_error is not None:
                    raise blocked_navigation_error from exc
                raise
            if response is not None and response.status >= 400:
                raise RuntimeError(f"http status {response.status}")
            final_url = normalize_link(page.url, page.url)
            await self.url_guard.validate(final_url)
            await self.wait_for_rendered_body(page, final_url)
            if self.args.wait_ms:
                await page.wait_for_timeout(self.args.wait_ms)
            final_url = normalize_link(page.url, page.url)
            await self.url_guard.validate(final_url)
            browser_body_text = await self.read_browser_body_text(page)
            html_text = await page.content()
            browser_links = await self.collect_browser_links(page, final_url) if need_links else []
            return parse_page(
                html_text,
                final_url,
                depth,
                need_links,
                self.plan.strategy,
                browser_links,
                browser_body_text,
                self.raw_dir,
                self.html_dir,
                self.index_base,
                self.args.body_char_limit,
                self.args.chunk_char_limit,
            )
        finally:
            await page.close()

    async def wait_for_rendered_body(self, page: Any, page_url: str) -> None:
        rule = rule_for_url(page_url)
        selector = rule.body_selector if rule and rule.body_selector else "article, main"
        if selector:
            try:
                await page.wait_for_selector(selector, state="attached", timeout=min(self.args.timeout_ms, 5000))
            except Exception:
                pass
        await self.wait_for_stable_body_text(page)

    async def read_browser_body_text(self, page: Any) -> str:
        try:
            return await page.locator("body").inner_text(timeout=5000)
        except Exception:
            return ""

    async def wait_for_stable_body_text(self, page: Any) -> None:
        deadline = asyncio.get_running_loop().time() + min(self.args.timeout_ms / 1000, 30000 / 1000)
        last_len = -1
        stable_count = 0
        while asyncio.get_running_loop().time() < deadline:
            text = clean_body_text(await self.read_browser_body_text(page))
            current_len = len(text)
            if current_len == last_len and current_len > 0:
                stable_count += 1
            else:
                stable_count = 0
            last_len = current_len
            if stable_count >= 2:
                return
            await page.wait_for_timeout(1000)

    async def collect_browser_links(self, page: Any, page_url: str) -> list[dict[str, str]]:
        rule = rule_for_url(page_url)
        if not rule or not rule.anchor_selectors:
            return []
        scroll_selectors = rule.anchor_scroll_selectors or ("body",)
        merged: list[dict[str, str]] = []
        for item_selector in rule.anchor_selectors:
            for scroll_selector in scroll_selectors:
                try:
                    raw_links = await page.evaluate(
                        BROWSER_LINK_SCRIPT,
                        {
                            "itemSelector": item_selector,
                            "scrollSelector": scroll_selector,
                            "linkTokenAttr": rule.link_token_attr,
                            "linkTokenParam": rule.link_token_param,
                            "linkURLTemplate": rule.link_url_template,
                            "linkTextSelector": rule.link_text_selector,
                        },
                    )
                except Exception as exc:
                    print(f"collect browser links failed: {exc}", file=sys.stderr)
                    continue
                links: list[dict[str, str]] = []
                for raw in raw_links:
                    raw_href = str(raw.get("raw_href", ""))
                    try:
                        normalized = normalize_link(raw_href, page_url)
                    except ValueError:
                        continue
                    links.append(
                        {
                            "url": normalized,
                            "raw_href": raw_href.strip(),
                            "text": collapse_space(str(raw.get("text", ""))),
                            "source": "directory",
                        }
                    )
                merged = merge_links(merged, links)
        return merged


def plan_payload(plan: CrawlPlan) -> dict[str, Any]:
    return {
        "crawl_strategy": plan.strategy,
        "depth": plan.depth,
        "decision_source": plan.decision_source,
        "reason": plan.reason,
        "needs_model_decision": plan.needs_model_decision,
    }


def body_excerpt_for_page(page_result: dict[str, Any], index_base: Path, limit: int = 4000) -> str:
    content_path = str(page_result.get("content_path") or "")
    if not content_path:
        return ""
    path = Path(content_path)
    if not path.is_absolute():
        path = index_base / path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text[:limit]


def build_probe_payload(
    start_url: str,
    page_result: dict[str, Any],
    plan: CrawlPlan,
    index_base: Path,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "start_url": start_url,
        "domain_rule": domain_rule_payload(rule_for_url(start_url)),
        "resolved_plan": plan_payload(plan),
        "model_decision_required": plan.needs_model_decision,
        "model_decision_options": [
            {
                "crawl_strategy": "directory",
                "recommended_depth": 1,
                "use_when": "The page exposes a directory, sidebar, catalog, wiki tree, or other table-of-contents links.",
            },
            {
                "crawl_strategy": "sequential-nav",
                "recommended_depth": "Use max-pages - 1, or another explicit hop limit large enough for the document chain.",
                "use_when": "The page has previous/next article or previous/next chapter navigation but no directory.",
            },
            {
                "crawl_strategy": "body-depth",
                "recommended_depth": 3,
                "use_when": "The page has neither directory links nor sequential navigation; follow links from the body text three levels deep.",
            },
        ],
        "page": {
            "url": page_result.get("url", ""),
            "final_url": page_result.get("final_url", ""),
            "title": page_result.get("title", ""),
            "description": page_result.get("description", ""),
            "keywords": page_result.get("keywords", []),
            "error": page_result.get("error", ""),
            "html_path": page_result.get("html_path", ""),
            "content_chars": page_result.get("content_chars", 0),
            "saved_content_chars": page_result.get("saved_content_chars", 0),
            "body_excerpt": body_excerpt_for_page(page_result, index_base),
            "link_evidence": page_result.get("link_evidence", {}),
        },
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    start_urls = load_start_urls(args)
    start_url = start_urls[0]
    plan = resolve_crawl_plan(args, start_url)
    out_dir, index_out, raw_dir, html_dir = crawl_output_paths(args)
    index_base = out_dir
    body_char_limit = DEFAULT_BODY_CHAR_LIMIT
    chunk_char_limit = DEFAULT_CHUNK_CHAR_LIMIT
    args.body_char_limit = body_char_limit
    args.chunk_char_limit = chunk_char_limit
    probe_payload: dict[str, Any] | None = None
    async with Crawler(args, raw_dir, html_dir, index_base, plan) as crawler:
        if plan.strategy == "batch":
            pages = await crawler.crawl_batch(start_urls)
        elif args.probe_only:
            probe_page = await crawler.probe(start_url)
            probe_payload = build_probe_payload(start_url, probe_page, plan, index_base)
            probe_path = out_dir / "probe.json"
            probe_path.parent.mkdir(parents=True, exist_ok=True)
            probe_path.write_text(json.dumps(probe_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if args.probe_only:
                pages = [probe_page]
            else:
                pages = await crawler.crawl(start_url)
        else:
            pages = await crawler.crawl(start_url)
        max_pages_truncated = crawler.max_pages_truncated
    return {
        "schema_version": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "crawl": {
            "start_url": start_url,
            "seed_urls": start_urls,
            "requested_depth": args.depth,
            "depth": plan.depth,
            "crawl_strategy": plan.strategy,
            "decision_source": plan.decision_source,
            "decision_reason": plan.reason,
            "needs_model_decision": plan.needs_model_decision,
            "probe_only": args.probe_only,
            "concurrency": args.concurrency,
            "max_pages": args.max_pages,
            "browser": args.browser,
            "link_scope": args.link_scope,
            "timeout_ms": args.timeout_ms,
            "wait_ms": args.wait_ms,
            "out_dir": str(out_dir),
            "body_char_limit": body_char_limit,
            "chunk_char_limit": chunk_char_limit,
            "html_dir": relpath(html_dir, index_base),
            "truncated_by_max_pages": max_pages_truncated,
        },
        "probe": probe_payload,
        "pages": pages,
    }


def main() -> int:
    args = parse_args()
    try:
        payload = asyncio.run(run(args))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _, index_out, _, _ = crawl_output_paths(args)
    index_out.parent.mkdir(parents=True, exist_ok=True)
    index_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if payload["crawl"].get("truncated_by_max_pages"):
        print(
            "warning: crawl reached --max-pages before all discovered URLs were scheduled; "
            "rerun with a higher --max-pages for a complete snapshot.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
