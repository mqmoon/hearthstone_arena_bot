import httpx
import json
from pathlib import Path
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg
from nonebot.log import logger
from nonebot.exception import FinishedException

DATA_DIR = Path(__file__).parent.parent.parent / "data" / "arena_bot_cn"
DATA_DIR.mkdir(parents=True, exist_ok=True)

arena_cmd = on_command("查竞技场", aliases={"查询竞技场"}, priority=10, block=True)
old_arena_cmd = on_command("查旧竞技场", aliases={"查询旧竞技场"}, priority=10, block=True)

SEASON_MAP = {
    "最新": "6",
    "S6": "6",
    "上赛季": "5",
    "S5": "5",
    "默认": "6",
    "S4": "4",
    "S3": "3",
    "S2": "2",
    "S1": "1",
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
    return season_id != SEASON_MAP["默认"]


def parse_args(user_input: str, is_old: bool = False):
    parts = user_input.split()
    default_season = "56" if is_old else "6"
    default_name = "56" if is_old else "最新赛季"
    default_limit = 10

    if not parts:
        return default_season, default_limit, default_name

    if is_old and len(parts) == 1 and parts[0].isdigit():
        num = int(parts[0])
        if 49 <= num <= 56:
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
        season_id = SEASON_MAP.get(season_key)
        season_name = season_key

    return season_id, limit, season_name


async def _fetch_page(client: httpx.AsyncClient, mode: str, season_id: str, page: int, use_cache: bool) -> list:
    cache_path = _cache_path(mode, season_id, page)

    if use_cache:
        cached = _load_cache(cache_path)
        if cached is not None:
            return cached

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
    players = data.get("data", {}).get("list", [])

    if use_cache and players:
        _save_cache(cache_path, players)

    return players


async def query_arena(matcher, mode: str, season_id: str, limit: int, season_name: str):
    use_cache = _should_cache(mode, season_id)
    pages_needed = (limit + 24) // 25

    all_players = []

    try:
        async with httpx.AsyncClient() as client:
            for page in range(1, pages_needed + 1):
                players = await _fetch_page(client, mode, season_id, page, use_cache)
                if not players:
                    break
                all_players.extend(players)
                if len(all_players) >= limit:
                    break

        if not all_players:
            await matcher.finish(f"未查询到【{season_name}】的榜单数据。")

        display = all_players[:limit]

        mode_name = "地下竞技场" if mode == "undergroundarena" else "旧版竞技场"
        reply_text = f"国服{mode_name}第{season_name}赛季榜单 (1-{len(display)}名)\n"

        for player in display:
            rank = player.get("position", 0)
            name = player.get("battle_tag", "隐藏玩家").split("#")[0]
            score = player.get("score", 0)
            reply_text += f"{rank} - {name} - {score}\n"

        reply_text = reply_text.rstrip("\n")
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
