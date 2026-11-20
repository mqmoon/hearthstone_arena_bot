#!/usr/bin/env python3
"""Backfill the arena leaderboard disk cache for complete leaderboards.

The bot caches each page (25 players) under data/arena_bot_cn/<mode>/season_<id>/page_<n>.json.
Historical seasons are cached forever (no TTL), so a season that was cached while it was
still live — and thus incomplete — stays incomplete forever. This script re-fetches the full
leaderboard for one or more seasons and records the API's ``total`` so the bot can detect
incomplete caches later.

Usage:
    python scripts/backfill_seasons.py                       # all undergroundarena seasons
    python scripts/backfill_seasons.py --mode arena          # all legacy arena seasons
    python scripts/backfill_seasons.py --seasons 7,8,9,10    # specific undergroundarena seasons
"""
import argparse
import json
import sys
from pathlib import Path

import httpx

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "arena_bot_cn"
PAGE_SIZE = 25
MAX_PAGES = 20  # 20 pages * 25 = 500 ranks

API_URL = "https://webapi.blizzard.cn/hs-rank-api-server/api/game/ranks"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://hs.blizzard.cn/",
}

NEW_SEASONS = [str(i) for i in range(1, 11)]  # undergroundarena
OLD_SEASONS = [str(i) for i in range(49, 57)]  # legacy arena


def fetch_page(client: httpx.Client, mode: str, season_id: str, page: int) -> tuple[list, int]:
    resp = client.get(
        API_URL,
        headers=HEADERS,
        params={
            "page": page,
            "page_size": PAGE_SIZE,
            "mode_name": mode,
            "season_id": season_id,
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    data = (resp.json().get("data") or {})
    return data.get("list", []), data.get("total", 0)


def backfill(mode: str, season_id: str) -> int:
    season_dir = DATA_DIR / mode / f"season_{season_id}"
    season_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    with httpx.Client() as client:
        for page in range(1, MAX_PAGES + 1):
            players, total = fetch_page(client, mode, season_id, page)
            (season_dir / f"page_{page}.json").write_text(
                json.dumps(players, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if not players:
                break

    (season_dir / "_total.json").write_text(
        json.dumps({"total": total}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="undergroundarena", choices=["undergroundarena", "arena"])
    parser.add_argument("--seasons", default=None, help="comma-separated season ids; default all for the mode")
    args = parser.parse_args()

    if args.seasons:
        ids = [s.strip() for s in args.seasons.split(",") if s.strip()]
    else:
        ids = NEW_SEASONS if args.mode == "undergroundarena" else OLD_SEASONS

    for sid in ids:
        total = backfill(args.mode, sid)
        print(f"{args.mode} season_{sid}: {total} players")

    return 0


if __name__ == "__main__":
    sys.exit(main())
