# -*- coding: utf-8 -*-
"""Run incremental investor Q&A collection on A-share trading afternoons.

This wrapper is intended for Windows Task Scheduler. It checks whether today is
an A-share trading day, then runs fetch_hudong_qa.py in all-incremental mode.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path


def is_trade_day(today: date) -> tuple[bool, str]:
    """Return whether today is an A-share trading day.

    Prefer akshare's Sina trading calendar. If unavailable, fall back to weekday
    check to avoid silently missing runs; the log message will show the fallback.
    """
    try:
        import akshare as ak  # type: ignore

        df = ak.tool_trade_date_hist_sina()
        days = {str(x)[:10] for x in df["trade_date"].tolist()}
        return today.isoformat() in days, "akshare.tool_trade_date_hist_sina"
    except Exception as exc:  # pragma: no cover - runtime safety fallback
        return today.weekday() < 5, f"weekday fallback because calendar failed: {exc!r}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scheduled incremental Q&A collector")
    parser.add_argument("--force", action="store_true", help="Run even if today is not a trading day.")
    parser.add_argument("--watchlist", default="自选股20260624.txt")
    parser.add_argument("--max-pages", default="100")
    parser.add_argument("--min-delay", default="0.8")
    parser.add_argument("--max-delay", default="2.2")
    args = parser.parse_args(argv)

    tools_dir = Path(__file__).resolve().parent
    root = tools_dir.parent
    os.chdir(root)

    now = datetime.now()
    ok, calendar_source = is_trade_day(now.date())
    print(f"[{now:%Y-%m-%d %H:%M:%S}] calendar={calendar_source}; trade_day={ok}; root={root}", flush=True)
    if not ok and not args.force:
        print("Not an A-share trading day; skipped. Use --force to override.", flush=True)
        return 0

    cmd = [
        sys.executable,
        str(tools_dir / "fetch_hudong_qa.py"),
        "--mode",
        "all-incremental",
        "--watchlist",
        args.watchlist,
        "--output-root",
        ".",
        "--max-pages",
        str(args.max_pages),
        "--min-delay",
        str(args.min_delay),
        "--max-delay",
        str(args.max_delay),
    ]
    print("Running: " + " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=root)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] collector exit code: {proc.returncode}", flush=True)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
