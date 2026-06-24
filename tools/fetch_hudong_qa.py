#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch investor-interaction Q&A for A-share watchlists and export to Obsidian.

Default source: Tonghuashun interactive API. It aggregates public Q&A for
Shanghai and Shenzhen listed companies and keeps a compact JSONP endpoint.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable


API_THS = "https://basic.10jqka.com.cn/interactive/api/list/"
REFERER_THS = "https://news.10jqka.com.cn/hudong/"
DEFAULT_WATCHLIST = "自选股20260624.txt"
DEFAULT_PAGE_SIZE = 20
DEFAULT_MAX_PAGES = 100
MODES = (
    "all-full",
    "stock-full",
    "stock-range",
    "all-incremental",
    "stock-incremental",
)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
]


@dataclass(frozen=True)
class Stock:
    code: str
    name: str
    market: str


@dataclass
class QAItem:
    id: str
    code: str
    name: str
    source: str
    market: str
    ask_user: str
    ask_time: str
    question: str
    answer_time: str
    answer: str
    raw: dict


def clean_text(value: object) -> str:
    text = "" if value is None else str(value)
    for _ in range(3):
        unescaped = html.unescape(text)
        if unescaped == text:
            break
        text = unescaped
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_dt(value: str) -> datetime | None:
    value = (value or "").strip()
    for fmt, width in (
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%d %H:%M", 16),
        ("%Y-%m-%d", 10),
    ):
        try:
            return datetime.strptime(value[:width], fmt)
        except ValueError:
            continue
    return None


def qa_sort_key(item: QAItem) -> str:
    return item.answer_time or item.ask_time or ""


def answer_date(item: QAItem) -> str:
    dt = parse_dt(item.answer_time)
    return dt.strftime("%Y-%m-%d") if dt else "unknown-date"


def market_of(code: str) -> str:
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return "SSE"
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return "SZSE"
    if code.startswith(("430", "83", "87", "88", "92")):
        return "BSE"
    if code.startswith(("159", "588", "510", "511", "512", "513", "515", "516", "517")):
        return "FUND"
    return "UNKNOWN"


def safe_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip())
    value = re.sub(r"\s+", "", value)
    return value or "unknown"


def read_watchlist(path: Path, include_funds: bool = False, include_bse: bool = False) -> list[Stock]:
    if not path.exists():
        raise FileNotFoundError(f"Watchlist not found: {path}")

    raw_bytes = path.read_bytes()
    last_error: UnicodeDecodeError | None = None
    text = ""
    for encoding in ("utf-8-sig", "gb18030", "gbk", "utf-16"):
        try:
            text = raw_bytes.decode(encoding)
            break
        except UnicodeDecodeError as exc:
            last_error = exc
    else:
        raise UnicodeDecodeError(
            last_error.encoding if last_error else "unknown",
            last_error.object if last_error else raw_bytes,
            last_error.start if last_error else 0,
            last_error.end if last_error else 1,
            f"Cannot decode watchlist file: {path}",
        )

    rows = text.splitlines()
    stocks: list[Stock] = []
    seen: set[str] = set()

    reader = csv.reader(rows, delimiter="\t")
    header_seen = False
    for row in reader:
        if not row or not row[0].strip():
            continue
        if row[0].startswith("#"):
            continue
        if row[0].strip() in {"代码", "code", "Code"}:
            header_seen = True
            continue
        if not header_seen and not re.fullmatch(r"\d{6}", row[0].strip()):
            continue

        code = row[0].strip()
        if not re.fullmatch(r"\d{6}", code):
            continue
        name = row[1].strip() if len(row) > 1 else code
        market = market_of(code)
        if market == "FUND" and not include_funds:
            continue
        if market == "BSE" and not include_bse:
            continue
        if market not in {"SSE", "SZSE", "BSE", "FUND"}:
            continue
        if code in seen:
            continue
        stocks.append(Stock(code=code, name=name, market=market))
        seen.add(code)
    return stocks


class HttpClient:
    def __init__(self, min_delay: float, max_delay: float, retries: int) -> None:
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.retries = retries
        self.opener = urllib.request.build_opener()

    def sleep(self, page: int) -> None:
        delay = random.uniform(self.min_delay, self.max_delay)
        if page > 1:
            delay += random.uniform(0.05, 0.4)
        time.sleep(delay)

    def get_text(self, url: str, referer: str, page: int) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            if attempt > 1:
                backoff = min(20.0, 1.8**attempt + random.random())
                time.sleep(backoff)
            self.sleep(page)
            headers = {
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Referer": referer,
                "User-Agent": random.choice(USER_AGENTS),
            }
            req = urllib.request.Request(url, headers=headers)
            try:
                with self.opener.open(req, timeout=30) as resp:
                    data = resp.read()
                    charset = resp.headers.get_content_charset() or "utf-8"
                    return data.decode(charset, errors="replace")
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
                last_error = exc
        raise RuntimeError(f"HTTP request failed after {self.retries} retries: {last_error}")


def strip_jsonp(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("callback(") and raw.endswith(")"):
        raw = raw[len("callback(") : -1]
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]
    return json.loads(raw)


def ths_url(code: str, page: int, page_size: int) -> str:
    params = {
        "top": 0,
        "totalcache": 1,
        "pagesize": page_size,
        "page": page,
        "code": code,
        "sort": "atime",
        "jsonp": "callback",
        "return": "jsonp",
        "true": "callback",
        "_": int(time.time() * 1000),
    }
    return API_THS + "?" + urllib.parse.urlencode(params)


def normalize_ths_item(stock: Stock, raw: dict) -> QAItem | None:
    if str(raw.get("isreply", "1")) == "0":
        return None
    question = clean_text(raw.get("question"))
    answer = clean_text(raw.get("answer"))
    if not question and not answer:
        return None
    ask_user = clean_text(raw.get("uid"))
    ask_user = ask_user.replace("投资者_", "投资者")
    seq = clean_text(raw.get("seq")) or f"{stock.code}-{clean_text(raw.get('qtime'))}-{question[:20]}"
    return QAItem(
        id=f"ths:{stock.code}:{seq}",
        code=stock.code,
        name=stock.name,
        source="同花顺互动问答",
        market=stock.market,
        ask_user=ask_user,
        ask_time=clean_text(raw.get("qtime")),
        question=question,
        answer_time=clean_text(raw.get("atime")),
        answer=answer,
        raw=raw,
    )


def fetch_stock_ths(
    stock: Stock,
    client: HttpClient,
    since: datetime | None,
    page_size: int,
    max_pages: int,
    full: bool,
) -> list[QAItem]:
    items: list[QAItem] = []
    stop_by_since = False

    for page in range(1, max_pages + 1):
        url = ths_url(stock.code, page, page_size)
        raw_text = client.get_text(url, REFERER_THS, page)
        try:
            payload = strip_jsonp(raw_text)
        except json.JSONDecodeError as exc:
            sample = raw_text[:160].replace("\n", " ")
            raise RuntimeError(f"Invalid JSONP for {stock.code} page {page}: {sample}") from exc

        page_items = payload.get("result") or []
        if not page_items:
            break

        for raw in page_items:
            item = normalize_ths_item(stock, raw)
            if item is None:
                continue
            dt = parse_dt(item.answer_time)
            if since and dt and dt < since:
                stop_by_since = True
                continue
            items.append(item)

        sys.stderr.write(f"{stock.code} {stock.name}: page {page}, +{len(page_items)} raw\n")

        if stop_by_since and not full:
            break
    return sorted(items, key=qa_sort_key, reverse=True)


def load_existing(stock_dir: Path) -> list[QAItem]:
    path = stock_dir / "data.json"
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    items: list[QAItem] = []
    for row in rows:
        raw = row.get("raw", {}) if isinstance(row, dict) else {}
        items.append(
            QAItem(
                id=clean_text(row.get("id", "")),
                code=clean_text(row.get("code", "")),
                name=clean_text(row.get("name", "")),
                source=clean_text(row.get("source", "")),
                market=clean_text(row.get("market", "")),
                ask_user=clean_text(row.get("ask_user", "")),
                ask_time=clean_text(row.get("ask_time", "")),
                question=clean_text(row.get("question", "")),
                answer_time=clean_text(row.get("answer_time", "")),
                answer=clean_text(row.get("answer", "")),
                raw=raw,
            )
        )
    return items


def merge_items(existing: Iterable[QAItem], fresh: Iterable[QAItem]) -> list[QAItem]:
    merged: dict[str, QAItem] = {}
    for item in existing:
        if item.id:
            merged[item.id] = item
    for item in fresh:
        if item.id:
            merged[item.id] = item
    return sorted(merged.values(), key=qa_sort_key, reverse=True)


def latest_answer_time(items: Iterable[QAItem]) -> datetime | None:
    times = [parse_dt(item.answer_time) for item in items]
    times = [dt for dt in times if dt is not None]
    return max(times) if times else None


def markdown_item(item: QAItem) -> str:
    lines = [
        f"### {item.answer_time or item.ask_time or 'unknown-time'}",
        "",
        f"- 来源: {item.source}",
        f"- 股票: {item.code} {item.name}",
        f"- 提问者: {item.ask_user or '未知'}",
        f"- 提问时间: {item.ask_time or '未知'}",
        f"- 答复时间: {item.answer_time or '未知'}",
        "",
        "**问:**",
        "",
        item.question or "无",
        "",
        "**答:**",
        "",
        item.answer or "无",
        "",
    ]
    return "\n".join(lines)


def write_outputs(root: Path, stock: Stock, items: list[QAItem]) -> Path:
    stock_dir = root / f"{stock.code}_{safe_filename(stock.name)}_互动问答"
    stock_dir.mkdir(parents=True, exist_ok=True)

    data_path = stock_dir / "data.json"
    data_path.write_text(
        json.dumps([asdict(item) for item in items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    by_date: dict[str, list[QAItem]] = {}
    for item in items:
        by_date.setdefault(answer_date(item), []).append(item)

    old_pages = list(stock_dir.glob("????-??-??.md")) + [stock_dir / "unknown-date.md"]
    for old_page in old_pages:
        if old_page.exists():
            try:
                old_page.unlink()
            except PermissionError:
                sys.stderr.write(f"Warning: cannot remove old generated page: {old_page}\n")

    index = [
        "---",
        f"stock_code: {stock.code}",
        f"stock_name: {stock.name}",
        "type: 互动问答汇总",
        "---",
        "",
        f"# {stock.code} {stock.name} 互动问答",
        "",
        f"- 更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 记录数: {len(items)}",
        "",
    ]
    for day, day_items in sorted(by_date.items(), reverse=True):
        index.append(f"## {day}")
        index.append("")
        for item in sorted(day_items, key=qa_sort_key, reverse=True):
            index.extend(
                [
                    f"### {item.answer_time or item.ask_time or 'unknown-time'}",
                    "",
                    f"- 提问者: {item.ask_user or '未知'}",
                    f"- 提问时间: {item.ask_time or '未知'}",
                    f"- 答复时间: {item.answer_time or '未知'}",
                    f"- 来源: {item.source}",
                    "",
                    "**问:**",
                    "",
                    item.question or "无",
                    "",
                    "**答:**",
                    "",
                    item.answer or "无",
                    "",
                    "---",
                    "",
                ]
            )
    summary_path = stock_dir / "00_问答汇总.md"
    legacy_summary_path = stock_dir / "问答汇总.md"
    summary_path.write_text("\n".join(index).rstrip() + "\n", encoding="utf-8")
    if legacy_summary_path.exists():
        try:
            legacy_summary_path.unlink()
        except PermissionError:
            sys.stderr.write(f"Warning: cannot remove old summary page: {legacy_summary_path}\n")
    return stock_dir



def wikilink_path(path: Path, root: Path) -> str:
    """Return an Obsidian-friendly wiki-link path without .md suffix."""
    rel = path.resolve().relative_to(root.resolve()).as_posix()
    if rel.endswith(".md"):
        rel = rel[:-3]
    return rel



def write_incremental_outputs(root: Path, run_date: str, incremental: dict[Stock, list[QAItem]]) -> None:
    """Write a daily incremental index and per-stock incremental detail notes."""
    non_empty = {stock: items for stock, items in incremental.items() if items}
    if not non_empty:
        sys.stderr.write("No fresh incremental Q&A; daily incremental note skipped.\n")
        return

    inc_dir = root / "增量问答"
    detail_dir = inc_dir / run_date
    detail_dir.mkdir(parents=True, exist_ok=True)

    index_path = inc_dir / f"增量问答_{run_date}.md"
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = sum(len(items) for items in non_empty.values())
    lines = [
        "---",
        f"date: {run_date}",
        "type: 增量互动问答索引",
        f"updated: {now_text}",
        f"stock_count: {len(non_empty)}",
        f"qa_count: {total}",
        "---",
        "",
        f"# 增量问答_{run_date}",
        "",
        f"- 更新时间: {now_text}",
        f"- 涉及股票数: {len(non_empty)}",
        f"- 新增问答数: {total}",
        "",
        "## 股票列表",
        "",
        "| 股票 | 新增问答数 | 最新答复时间 | 增量明细 | 完整汇总 |",
        "|---|---:|---|---|---|",
    ]

    for stock, items in sorted(non_empty.items(), key=lambda kv: kv[0].code):
        sorted_items = sorted(items, key=qa_sort_key, reverse=True)
        stock_dir_name = f"{stock.code}_{safe_filename(stock.name)}_互动问答"
        detail_path = detail_dir / f"{stock.code}_{safe_filename(stock.name)}_增量问答_{run_date}.md"
        summary_path = root / stock_dir_name / "00_问答汇总.md"
        detail_lines = [
            "---",
            f"date: {run_date}",
            f"stock_code: {stock.code}",
            f"stock_name: {stock.name}",
            "type: 股票增量互动问答",
            f"updated: {now_text}",
            f"qa_count: {len(sorted_items)}",
            "---",
            "",
            f"# {stock.code} {stock.name} 增量问答_{run_date}",
            "",
            f"- 更新时间: {now_text}",
            f"- 新增问答数: {len(sorted_items)}",
            f"- 完整汇总: [[{wikilink_path(summary_path, root)}]]",
            "",
        ]
        for item in sorted_items:
            detail_lines.extend([
                f"## {item.answer_time or item.ask_time or 'unknown-time'}",
                "",
                f"- 来源: {item.source}",
                f"- 提问者: {item.ask_user or '未知'}",
                f"- 提问时间: {item.ask_time or '未知'}",
                f"- 答复时间: {item.answer_time or '未知'}",
                "",
                "**问:**",
                "",
                item.question or "无",
                "",
                "**答:**",
                "",
                item.answer or "无",
                "",
                "---",
                "",
            ])
        detail_path.write_text("\n".join(detail_lines).rstrip() + "\n", encoding="utf-8")

        latest = sorted_items[0].answer_time or sorted_items[0].ask_time or ""
        lines.append(
            f"| {stock.code} {stock.name} | {len(sorted_items)} | {latest} | "
            f"[[{wikilink_path(detail_path, root)}|查看增量]] | "
            f"[[{wikilink_path(summary_path, root)}|完整汇总]] |"
        )

    lines.extend(["", "## 说明", "", "本页由每日增量采集任务自动生成；仅列出本次运行新抓取到的互动问答。", ""])
    index_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    sys.stderr.write(f"Daily incremental note saved -> {index_path}\n")

def choose_stocks(stocks: list[Stock], code: str | None, name: str | None, limit: int | None) -> list[Stock]:
    result = stocks
    if code:
        wanted = {part.strip() for part in code.split(",") if part.strip()}
        result = [stock for stock in result if stock.code in wanted]
    if name:
        result = [stock for stock in result if name in stock.name]
    if limit:
        result = result[:limit]
    return result


def period_to_days(period: str | None) -> int | None:
    if not period:
        return None
    value = period.strip().lower()
    aliases = {
        "1w": 7,
        "7d": 7,
        "week": 7,
        "weekly": 7,
        "一周": 7,
        "近一周": 7,
        "近1周": 7,
        "1m": 30,
        "30d": 30,
        "month": 30,
        "monthly": 30,
        "一月": 30,
        "一个月": 30,
        "近一月": 30,
        "近1月": 30,
    }
    if value in aliases:
        return aliases[value]
    match = re.fullmatch(r"(\d+)([dwm])", value)
    if not match:
        raise ValueError(f"Invalid --period value: {period}. Use examples like 1w, 1m, 7d, 30d.")
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "d":
        return amount
    if unit == "w":
        return amount * 7
    return amount * 30


def apply_mode_defaults(args: argparse.Namespace, parser: argparse.ArgumentParser) -> argparse.Namespace:
    period_days = period_to_days(args.period)
    if period_days and not args.days:
        args.days = period_days

    if not args.mode:
        return args

    stock_mode = args.mode.startswith("stock-")
    if stock_mode and not args.code:
        parser.error(f"--mode {args.mode} requires --code")

    if args.mode in {"all-full", "stock-full"}:
        args.full = True
        args.incremental = False
    elif args.mode == "stock-range":
        args.full = False
        args.incremental = False
        args.replace = True
        if not args.days and not args.since:
            args.days = 7
    elif args.mode in {"all-incremental", "stock-incremental"}:
        args.full = False
        args.incremental = True
    return args

def build_since(args: argparse.Namespace, existing: list[QAItem]) -> datetime | None:
    candidates: list[datetime] = []
    if args.since:
        parsed = parse_dt(args.since)
        if not parsed:
            raise ValueError(f"Invalid --since value: {args.since}")
        candidates.append(parsed)
    if args.days:
        candidates.append(datetime.now() - timedelta(days=args.days))
    if args.incremental and existing:
        latest = latest_answer_time(existing)
        if latest:
            candidates.append(latest)
    if not candidates:
        return None
    return max(candidates)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch public investor Q&A and export Obsidian notes.")
    parser.add_argument("--watchlist", default=DEFAULT_WATCHLIST, help="Path to watchlist txt exported from TDX.")
    parser.add_argument("--output-root", default=".", help="Obsidian StockResearch directory.")
    parser.add_argument(
        "--mode",
        choices=MODES,
        help=(
            "Command endpoint: all-full, stock-full, stock-range, "
            "all-incremental, stock-incremental."
        ),
    )
    parser.add_argument("--period", help="Time window alias, for example 1w, 1m, 7d, 30d.")
    parser.add_argument("--code", help="Comma-separated stock codes, for example 688456,300034.")
    parser.add_argument("--name", help="Filter by stock name substring.")
    parser.add_argument("--limit-stocks", type=int, help="Only process the first N matched stocks.")
    parser.add_argument("--days", type=int, help="Fetch items answered in the latest N days.")
    parser.add_argument("--since", help="Fetch items answered since YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--full", action="store_true", help="Ignore incremental stop and fetch up to --max-pages.")
    parser.add_argument("--incremental", action="store_true", help="Use existing data.json latest answer time as lower bound.")
    parser.add_argument("--replace", action="store_true", help="Replace data.json with this run instead of merging existing data.")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--min-delay", type=float, default=0.8)
    parser.add_argument("--max-delay", type=float, default=2.2)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--include-funds", action="store_true", help="Also process ETF/fund-like codes.")
    parser.add_argument("--include-bse", action="store_true", help="Also process Beijing Stock Exchange codes.")
    args = parser.parse_args(argv)
    return apply_mode_defaults(args, parser)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = Path(args.output_root).resolve()
    watchlist = Path(args.watchlist)
    if not watchlist.is_absolute():
        watchlist = root / watchlist

    stocks = read_watchlist(watchlist, include_funds=args.include_funds, include_bse=args.include_bse)
    stocks = choose_stocks(stocks, args.code, args.name, args.limit_stocks)
    if not stocks:
        sys.stderr.write("No stocks matched.\n")
        return 2

    client = HttpClient(min_delay=args.min_delay, max_delay=args.max_delay, retries=args.retries)
    sys.stderr.write(f"Matched {len(stocks)} stock(s). Output root: {root}\n")
    run_date = datetime.now().strftime("%Y%m%d")
    incremental_by_stock: dict[Stock, list[QAItem]] = {}

    for stock in stocks:
        stock_dir = root / f"{stock.code}_{safe_filename(stock.name)}_互动问答"
        existing = load_existing(stock_dir)
        since = build_since(args, existing)
        since_text = since.strftime("%Y-%m-%d %H:%M:%S") if since else "beginning"
        sys.stderr.write(f"\nFetching {stock.code} {stock.name} ({stock.market}), since {since_text}\n")

        fresh = fetch_stock_ths(
            stock=stock,
            client=client,
            since=since,
            page_size=args.page_size,
            max_pages=args.max_pages,
            full=args.full,
        )
        existing_ids = {item.id for item in existing if item.id}
        fresh_new = [item for item in fresh if item.id and item.id not in existing_ids]
        merged = sorted(fresh, key=qa_sort_key, reverse=True) if args.replace else merge_items(existing, fresh)
        out_dir = write_outputs(root, stock, merged)
        incremental_by_stock[stock] = fresh_new
        mode = "replace" if args.replace else "merge"
        sys.stderr.write(f"Saved {len(merged)} item(s), fresh {len(fresh)}, new {len(fresh_new)}, mode {mode} -> {out_dir}\n")

    if args.incremental and not args.replace:
        write_incremental_outputs(root, run_date, incremental_by_stock)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

