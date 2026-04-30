import httpx
import json
import asyncio
from pathlib import Path
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg
from nonebot.log import logger
from nonebot.exception import FinishedException

DATA_DIR = Path(__file__).parent.parent.parent / "data" / "arena_bot_cn"
DATA_DIR.mkdir(parents=True, exist_ok=True)

arena_cmd = on_command("查竞技场", aliases={"查询竞技场", "rank", "new"}, priority=10, block=True)
old_arena_cmd = on_command("查旧竞技场", aliases={"查询旧竞技场", "legacy", "old"}, priority=10, block=True)
career_cmd = on_command("生涯", aliases={"career"}, priority=10, block=True)
tutorial_cmd = on_command("tutorial", aliases={"教程", "帮助", "help", "指引"}, priority=10, block=True)

TUTORIAL_TEXT = """【竞技场查询 Bot 使用指引】

新版地下竞技场（查竞技场 / rank / new）
  查竞技场            → 最新赛季前10名
  查竞技场 6          → 第6赛季前10名
  查竞技场 5 50       → 第5赛季前50名
  赛季：1~6，别名：最新、上赛季

旧版竞技场（查旧竞技场 / legacy / old）
  查旧竞技场          → 第56赛季前10名
  查旧竞技场 56       → 第56赛季前10名
  查旧竞技场 49 100   → 第49赛季前100名
  赛季：49~56，别名：最新、上赛季、默认

玩家生涯（生涯 / career）
  生涯 玩家名         → 该玩家所有赛季的榜单记录

提示：最后一个数字为排名上限，Bot 自动分页获取。旧版及非最新新版赛季数据会被缓存。"""

ALL_NEW_SEASONS = ["1", "2", "3", "4", "5", "6"]
ALL_OLD_SEASONS = [str(i) for i in range(49, 57)]  # 49-56

SEASON_ALIASES = {
    "最新": "6",
    "上赛季": "5",
    "默认": "6",
}

OLD_SEASON_ALIASES = {
    "默认": "56",
    "最新": "56",
    "上赛季": "55",
}


def _cache_path(mode: str, season_id: str, page: int) -> Path:
    d = DATA_DIR / mode / f"season_{season_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"page_{page}.json"


def _load_cache(path: Path) -> list | None:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(path: Path, players: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(players, f, ensure_ascii=False, indent=2)


def _should_cache(mode: str, season_id: str) -> bool:
    if mode == "arena":
        return True
    return season_id != SEASON_ALIASES["默认"]


def parse_args(user_input: str, is_old: bool = False):
    parts = user_input.split()
    default_season = "56" if is_old else "6"
    default_name = "56" if is_old else "最新赛季"
    default_limit = 10

    if not parts:
        return default_season, default_limit, default_name

    # 单数字特判：优先视为赛季ID
    if len(parts) == 1 and parts[0].isdigit():
        num = int(parts[0])
        if is_old and 49 <= num <= 56:
            return str(num), default_limit, str(num)
        if not is_old and 1 <= num <= 6:
            return str(num), default_limit, str(num)

    limit = default_limit
    if parts[-1].isdigit():
        limit = max(1, int(parts[-1]))
        season_parts = parts[:-1]
    else:
        season_parts = parts

    season_key = " ".join(season_parts).strip().upper()

    if not season_key:
        return default_season, limit, default_name

    if is_old:
        season_id = OLD_SEASON_ALIASES.get(season_key)
        season_name = season_key
        if not season_id:
            if season_key.isdigit() and 49 <= int(season_key) <= 56:
                season_id = season_key
            else:
                season_id = None
    else:
        season_id = SEASON_ALIASES.get(season_key)
        season_name = season_key
        if not season_id:
            if season_key.isdigit() and 1 <= int(season_key) <= 6:
                season_id = season_key
            else:
                season_id = None

    return season_id, limit, season_name


async def _fetch_page_from_api(client: httpx.AsyncClient, mode: str, season_id: str, page: int) -> list:
    api_url = (
        f"https://webapi.blizzard.cn/hs-rank-api-server/api/game/ranks"
        f"?page={page}&page_size=25&mode_name={mode}&season_id={season_id}"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://hs.blizzard.cn/",
    }
    response = await client.get(api_url, headers=headers, timeout=10.0)
    data = response.json()
    return data.get("data", {}).get("list", [])


async def _fetch_all_pages(mode: str, season_id: str, pages_needed: int, use_cache: bool) -> list:
    all_players: list[dict] = []

    # 1. 区分已缓存和未缓存的页
    cached_pages: list[int] = []
    uncached_pages: list[int] = []

    if use_cache:
        for page in range(1, pages_needed + 1):
            if _cache_path(mode, season_id, page).exists():
                cached_pages.append(page)
            else:
                uncached_pages.append(page)
    else:
        uncached_pages = list(range(1, pages_needed + 1))

    # 2. 并发读取所有已缓存的页（核心优化：串行 -> 并发）
    if cached_pages:
        tasks = [
            (page, asyncio.to_thread(_load_cache, _cache_path(mode, season_id, page)))
            for page in cached_pages
        ]
        results = await asyncio.gather(*[t[1] for t in tasks])
        # 按页码排序后合并，保证顺序正确
        for page, players in sorted(zip([t[0] for t in tasks], results), key=lambda x: x[0]):
            if players:
                all_players.extend(players)

    # 3. 串行请求未缓存的页（避免触发 API 限流）
    if uncached_pages:
        async with httpx.AsyncClient() as client:
            for page in uncached_pages:
                players = await _fetch_page_from_api(client, mode, season_id, page)
                if players:
                    if use_cache:
                        await asyncio.to_thread(
                            _save_cache, _cache_path(mode, season_id, page), players
                        )
                    all_players.extend(players)
                if not players:
                    break

    return all_players


async def _get_cached_season_all(mode: str, season_id: str) -> list[dict]:
    cache_dir = DATA_DIR / mode / f"season_{season_id}"
    if not cache_dir.exists():
        return []

    page_files = sorted(
        cache_dir.glob("page_*.json"),
        key=lambda p: int(p.stem.split("_")[1]),
    )
    if not page_files:
        return []

    tasks = [asyncio.to_thread(_load_cache, f) for f in page_files]
    results = await asyncio.gather(*tasks)

    all_players: list[dict] = []
    for players in results:
        if players:
            all_players.extend(players)
    return all_players


def _rank_prefix(position: int) -> str:
    if position <= 10:
        return "[前10] "
    if position <= 25:
        return "[前25] "
    if position <= 50:
        return "[前50] "
    if position <= 100:
        return "[前100]"
    return " " * 11


async def query_career(matcher, player_id: str):
    # 并发获取所有赛季数据：旧版 49-56 + 新版 1-6
    old_tasks = [_get_cached_season_all("arena", sid) for sid in ALL_OLD_SEASONS]
    new_tasks = [_get_cached_season_all("undergroundarena", sid) for sid in ALL_NEW_SEASONS]

    old_results = await asyncio.gather(*old_tasks)
    new_results = await asyncio.gather(*new_tasks)

    records: list[dict] = []

    for season_id, players in zip(ALL_OLD_SEASONS, old_results):
        for player in players:
            tag = player.get("battle_tag", "")
            if tag.split("#")[0].lower() == player_id.lower():
                records.append({
                    "mode": "旧版",
                    "season": season_id,
                    "position": player.get("position", 0),
                    "score": player.get("score", 0),
                })

    for season_id, players in zip(ALL_NEW_SEASONS, new_results):
        for player in players:
            tag = player.get("battle_tag", "")
            if tag.split("#")[0].lower() == player_id.lower():
                records.append({
                    "mode": "地下",
                    "season": season_id,
                    "position": player.get("position", 0),
                    "score": player.get("score", 0),
                })

    if not records:
        await matcher.finish(f"未找到玩家【{player_id}】的榜单记录。")

    # 按时间排序（从旧到新）：旧版 49-56 -> 新版 1-6
    def sort_key(r: dict) -> tuple:
        if r["mode"] == "旧版":
            return (0, int(r["season"]))
        return (1, int(r["season"]))

    records.sort(key=sort_key)

    lines = [f"玩家【{player_id}】的竞技场生涯记录"]
    for r in records:
        prefix = _rank_prefix(r["position"])
        lines.append(f"{prefix}{r['mode']} S{r['season']} - 第{r['position']}名 - {r['score']}")

    reply_text = "\n".join(lines)
    await matcher.finish(reply_text)


async def query_arena(matcher, mode: str, season_id: str, limit: int, season_name: str):
    use_cache = _should_cache(mode, season_id)
    pages_needed = (limit + 24) // 25

    try:
        all_players = await _fetch_all_pages(mode, season_id, pages_needed, use_cache)

        if not all_players:
            await matcher.finish(f"未查询到【{season_name}】的榜单数据。")

        display = all_players[:limit]

        mode_name = "地下竞技场" if mode == "undergroundarena" else "旧版竞技场"

        # 列表 join 替代 +=，避免循环中大量临时字符串
        lines = [f"国服{mode_name}第{season_name}赛季榜单 (1-{len(display)}名)"]
        for player in display:
            rank = player.get("position", 0)
            name = player.get("battle_tag", "隐藏玩家").split("#")[0]
            score = player.get("score", 0)
            lines.append(f"{rank} - {name} - {score}")

        reply_text = "\n".join(lines)
        await matcher.finish(reply_text)

    except FinishedException:
        raise
    except httpx.TimeoutException:
        await matcher.finish("⏳ 请求官方接口超时，请稍后再试。")
    except Exception as e:
        logger.error(f"竞技场查询报错: {e}")
        await matcher.finish("❌ 查询发生未知错误，请联系管理员。")


@arena_cmd.handle()
async def handle_arena_rank(event: MessageEvent, args: Message = CommandArg()):
    user_input = args.extract_plain_text().strip()
    season_id, limit, season_name = parse_args(user_input, is_old=False)

    if not season_id:
        await arena_cmd.finish("未找到对应的赛季参数，请尝试输入如：查竞技场 最新")

    await query_arena(arena_cmd, "undergroundarena", season_id, limit, season_name)


@old_arena_cmd.handle()
async def handle_old_arena_rank(event: MessageEvent, args: Message = CommandArg()):
    user_input = args.extract_plain_text().strip()
    season_id, limit, season_name = parse_args(user_input, is_old=True)

    if not season_id:
        await old_arena_cmd.finish("未找到对应的赛季参数，旧版竞技场赛季范围为 49~56")

    await query_arena(old_arena_cmd, "arena", season_id, limit, season_name)


@career_cmd.handle()
async def handle_career(event: MessageEvent, args: Message = CommandArg()):
    user_input = args.extract_plain_text().strip()
    if not user_input:
        await career_cmd.finish("请输入玩家ID，如：生涯 玩家名")

    await query_career(career_cmd, user_input)


@tutorial_cmd.handle()
async def handle_tutorial(event: MessageEvent, args: Message = CommandArg()):
    await tutorial_cmd.finish(TUTORIAL_TEXT)
