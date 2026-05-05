import httpx
import json
import time
import asyncio
import threading
from pathlib import Path
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg
from nonebot.log import logger
from nonebot.exception import FinishedException

from arena_bot_cn.plugins.bind_account import _fuzzy_find_bindings, _find_binding_by_game_id, _get_bindings_for_qq

DATA_DIR = Path(__file__).parent.parent.parent / "data" / "arena_bot_cn"
DATA_DIR.mkdir(parents=True, exist_ok=True)

CACHE_TTL = 600  # 10-minutes disk cache TTL for the latest underground arena season
MAX_CONCURRENT = 20  # max parallel API requests
MAX_PLAYER_PAGES = 20  # pages to search when looking up a player

_mem_cache: dict[tuple, tuple[float, list]] = {}  # (mode, season_id, page) -> (timestamp, players)
_mem_cache_lock = threading.Lock()


def _resolve_rank_player_input(token: str, qq: str) -> tuple[list[str], bool]:
    """Resolve a player token to game IDs for rank/legacy search.

    Exact match (own binding) -> fuzzy match (own bindings) -> raw token fallback.
    Returns (game_ids, from_binding) where from_binding is True if any binding was matched.
    """
    token = token.strip()
    if not token:
        return [], False

    token_lower = token.lower()

    # Exact match — only if it's the user's own binding
    exact = _find_binding_by_game_id(token_lower)
    if exact is not None and exact["qq"] == qq:
        return [exact["name"]], True

    # Fuzzy match: token is a substring of the user's own game_ids
    fuzzy = _fuzzy_find_bindings(token, qq)
    if fuzzy:
        # Deduplicate by display name
        seen = set()
        result = []
        for b in fuzzy:
            if b["name"] not in seen:
                seen.add(b["name"])
                result.append(b["name"])
        return result, True

    # Fallback: search API with raw token
    return [token], False


arena_cmd = on_command("查竞技场", aliases={"查询竞技场", "rank", "new"}, priority=10, block=True)
old_arena_cmd = on_command("查旧竞技场", aliases={"查询旧竞技场", "legacy", "old"}, priority=10, block=True)
career_cmd = on_command("生涯", aliases={"career"}, priority=10, block=True)
tutorial_cmd = on_command("tutorial", aliases={"教程", "帮助", "help", "指引"}, priority=10, block=True)
cutoff_cmd = on_command("分数线", aliases={"cutoff", "cutoffs"}, priority=10, block=True)

TUTORIAL_TEXT = """【竞技场查询 Bot 使用指引】

地下竞技场（查竞技场 / rank / new）  赛季：1~6，别名：最新、上赛季
  查竞技场                   → 最新赛季前10名
  查竞技场 6                → 第6赛季前10名
  查竞技场 100              → 最新赛季前100名
  查竞技场 5 50             → 第5赛季前50名
  查竞技场 玩家名           → 查询玩家排名（支持模糊匹配）
  查竞技场 6 玩家名         → 指定赛季查询玩家
  查竞技场 A,B,C            → 多ID查询（, 或 ，分隔）

旧版竞技场（查旧竞技场 / legacy / old）  赛季：49~56，别名：最新、上赛季、默认
  同上用法，赛季范围不同。

分数线（分数线 / cutoff / cutoffs）
  分数线                    → 最新赛季前10/25/50/100/500分数线
  分数线 5                  → 第5赛季分数线

玩家生涯（生涯 / career）
  生涯 玩家名               → 该玩家所有赛季排名记录

账号绑定（账号绑定 / bind）
  账号绑定                   → 列出已绑定账号
  账号绑定 游戏ID            → 绑定账号
  账号绑定 删除 游戏ID       → 解除绑定
  注意：同一游戏ID只能被一个QQ绑定。

战绩记录（战绩上传 / 我的战绩 / 删除战绩 / 清空战绩）
  战绩上传 游戏ID 胜场…     → 上传胜场（最多30轮，0~12）
  我的战绩                   → 查看首个绑定账号战绩
  删除战绩 游戏ID 轮次…     → 删除指定轮次
  清空战绩 [游戏ID]          → 清空全部战绩（默认首个绑定账号）

提示：玩家名支持模糊匹配（绑定账号子串匹配 + 榜单全量搜索）；
多ID以逗号分隔；数字参数默认为排名上限。"""

ALL_NEW_SEASONS = ["1", "2", "3", "4", "5", "6"]
ALL_OLD_SEASONS = [str(i) for i in range(49, 57)]  # 49-56

_latest_new = max(ALL_NEW_SEASONS, key=int)
_latest_old = max(ALL_OLD_SEASONS, key=int)

SEASON_ALIASES = {
    "最新": _latest_new,
    "上赛季": str(int(_latest_new) - 1),
    "默认": _latest_new,
}

OLD_SEASON_ALIASES = {
    "默认": _latest_old,
    "最新": _latest_old,
    "上赛季": str(int(_latest_old) - 1),
}


def _cache_path(mode: str, season_id: str, page: int) -> Path:
    d = DATA_DIR / mode / f"season_{season_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"page_{page}.json"


def _load_cache(path: Path, ttl: int | None = None) -> list | None:
    cache_key = _path_to_cache_key(path)

    with _mem_cache_lock:
        if cache_key and cache_key in _mem_cache:
            ts, data = _mem_cache[cache_key]
            if ttl is None or time.time() - ts < ttl:
                return data
            del _mem_cache[cache_key]

    if not path.exists():
        return None
    try:
        mtime = path.stat().st_mtime
        if ttl is not None and time.time() - mtime > ttl:
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if cache_key:
            with _mem_cache_lock:
                _mem_cache[cache_key] = (time.time(), data)
        return data
    except Exception:
        return None


def _save_cache(path: Path, players: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(players, f, ensure_ascii=False, indent=2)
    cache_key = _path_to_cache_key(path)
    if cache_key:
        with _mem_cache_lock:
            _mem_cache[cache_key] = (time.time(), players)


def _path_to_cache_key(path: Path) -> tuple | None:
    try:
        season_dir = path.parent.name  # "season_6"
        mode = path.parent.parent.name  # "undergroundarena"
        page = int(path.stem.split("_")[1])  # "page_1" -> 1
        season_id = season_dir.split("_", 1)[1]
        return (mode, season_id, page)
    except (IndexError, ValueError):
        return None


def _find_player_in_page(players: list, player_name: str) -> dict | None:
    """Search for a player by exact name match first, then substring match."""
    name_lower = player_name.lower()
    # Exact match first
    for p in players:
        tag = p.get("battle_tag", "")
        if tag.split("#")[0].lower() == name_lower:
            return p
    # Substring match
    for p in players:
        tag = p.get("battle_tag", "")
        if name_lower in tag.split("#")[0].lower():
            return p
    return None


def _find_all_players_in_page(players: list, player_name: str) -> list[dict]:
    """Return ALL players whose name contains player_name (case-insensitive substring)."""
    name_lower = player_name.lower()
    result = []
    for p in players:
        tag = p.get("battle_tag", "")
        if name_lower in tag.split("#")[0].lower():
            result.append(p)
    return result


def _resolve_cache_ttl(mode: str, season_id: str) -> int | None:
    if mode == "undergroundarena" and season_id == SEASON_ALIASES["默认"]:
        return CACHE_TTL
    return None


async def _search_player_in_season(mode: str, season_id: str, player_name: str) -> dict | None:
    """Fetch pages one at a time, searching for a player. Stops on first match."""
    ttl = _resolve_cache_ttl(mode, season_id)
    async with httpx.AsyncClient() as client:
        for page in range(1, MAX_PLAYER_PAGES + 1):
            cache_path = _cache_path(mode, season_id, page)
            players = _load_cache(cache_path, ttl=ttl)

            if players is None:
                players = await _fetch_page_from_api(client, mode, season_id, page)
                if players:
                    await asyncio.to_thread(_save_cache, cache_path, players)
                else:
                    return None

            match = _find_player_in_page(players, player_name)
            if match:
                return match

    return None


async def _search_all_players_in_season(mode: str, season_id: str, player_name: str) -> list[dict]:
    """Scan ALL pages, collecting every player whose name contains player_name."""
    ttl = _resolve_cache_ttl(mode, season_id)
    all_matches: list[dict] = []
    async with httpx.AsyncClient() as client:
        for page in range(1, MAX_PLAYER_PAGES + 1):
            cache_path = _cache_path(mode, season_id, page)
            players = _load_cache(cache_path, ttl=ttl)

            if players is None:
                players = await _fetch_page_from_api(client, mode, season_id, page)
                if players:
                    await asyncio.to_thread(_save_cache, cache_path, players)
                else:
                    break

            matches = _find_all_players_in_page(players, player_name)
            all_matches.extend(matches)

    return all_matches


def _should_cache(mode: str, season_id: str) -> bool:
    return True


def _comma_split(s: str | None) -> list[str]:
    if not s:
        return []
    return [n.strip() for n in s.replace("，", ",").split(",") if n.strip()]


def parse_args(user_input: str, is_old: bool = False):
    parts = user_input.split()
    default_season = "56" if is_old else "6"
    default_name = "56" if is_old else "最新赛季"
    default_limit = 10
    player_names: list[str] = []

    if not parts:
        return default_season, default_limit, default_name, player_names

    # 单数字：视为赛季ID；不在合法赛季范围则视为排名上限
    if len(parts) == 1 and parts[0].isdigit():
        num = int(parts[0])
        if is_old and 49 <= num <= 56:
            return str(num), default_limit, str(num), player_names
        if not is_old and 1 <= num <= 6:
            return str(num), default_limit, str(num), player_names
        # 数字不在赛季范围 -> 视为排名上限（如 rank 100 = 最新赛季前100名）
        return default_season, max(1, num), default_name, player_names

    # 单参数非数字：赛季别名 -> 赛季，否则视为玩家名
    if len(parts) == 1:
        aliases = OLD_SEASON_ALIASES if is_old else SEASON_ALIASES
        if parts[0] in aliases:
            sid = aliases[parts[0]]
            return sid, default_limit, parts[0], player_names
        # 不是合法赛季参数，视为玩家名
        return default_season, default_limit, default_name, _comma_split(parts[0])

    limit = default_limit
    # 最后一个参数为数字 -> 排名上限；否则视为玩家名
    if parts[-1].isdigit():
        limit = max(1, int(parts[-1]))
        season_parts = parts[:-1]
    else:
        player_names = _comma_split(parts[-1])
        season_parts = parts[:-1]

    season_key = " ".join(season_parts).strip().upper()

    if not season_key:
        return default_season, limit, default_name, player_names

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

    return season_id, limit, season_name, player_names


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
    players_data = data.get("data") or {}
    return players_data.get("list", [])


async def _fetch_all_pages(mode: str, season_id: str, pages_needed: int, use_cache: bool, ttl: int | None = None) -> list:
    all_players: list[dict] = []
    uncached_pages: list[int] = []

    for page in range(1, pages_needed + 1):
        if use_cache:
            players = _load_cache(_cache_path(mode, season_id, page), ttl)
        else:
            players = None

        if players is not None:
            all_players.extend(players)
        else:
            uncached_pages.append(page)

    if uncached_pages:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT)

        async def _fetch_one(page: int):
            async with semaphore:
                async with httpx.AsyncClient() as client:
                    players = await _fetch_page_from_api(client, mode, season_id, page)
                if players and use_cache:
                    await asyncio.to_thread(
                        _save_cache, _cache_path(mode, season_id, page), players
                    )
                return players

        results = await asyncio.gather(*[_fetch_one(p) for p in uncached_pages])
        for players in results:
            if players:
                all_players.extend(players)

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

    tasks = [asyncio.to_thread(_load_cache, f, None) for f in page_files]
    results = await asyncio.gather(*tasks)

    all_players: list[dict] = []
    for players in results:
        if players:
            all_players.extend(players)
    return all_players


def _rank_prefix(position: int) -> str:
    if position <= 10:                                                                                                                             
        return f"[第{position}] "                                                                                                                                                                                        
    if position <= 25:                                                                                                                                                                                                   
        return "[前25] "                                                                                                                                                                                                 
    if position <= 50:                                                                                                                                                                                                   
        return "[前50] "                                                                                                                                                                                                 
    if position <= 100:                                                                                                                                                                                                  
        return "[前100] "
    return " " * 11


def _season_display(season_name: str) -> str:
    """Format season name: numeric → '第X赛季', alias → as-is."""
    if season_name.isdigit():
        return f"第{season_name}赛季"
    return season_name


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


async def query_arena(matcher, mode: str, season_id: str, limit: int, season_name: str, player_names: list[str] = None, qq: str = ""):
    use_cache = _should_cache(mode, season_id)
    cache_ttl = _resolve_cache_ttl(mode, season_id)
    mode_display = "地下竞技场" if mode == "undergroundarena" else "旧版竞技场"

    try:
        if player_names:
            # Resolve each name through bindings.
            # - Exact binding match → search for the resolved name exactly (find-one)
            # - Fuzzy binding match → search original token as substring (find-all)
            # - No binding match → search original token as substring (find-all)
            exact_search_ids: list[str] = []
            raw_fallback_tokens: list[str] = []
            seen = set()

            for name in player_names:
                ids, from_binding = _resolve_rank_player_input(name, qq)
                name_lower = name.strip().lower()

                if from_binding and all(rid.lower() == name_lower for rid in ids):
                    # Exact binding match — search for resolved names exactly
                    for rid in ids:
                        if rid.lower() not in seen:
                            seen.add(rid.lower())
                            exact_search_ids.append(rid)
                else:
                    # Fuzzy or no match — use original token for find-all
                    token = name.strip()
                    if token and token.lower() not in seen:
                        seen.add(token.lower())
                        raw_fallback_tokens.append(token)

            # Collect results
            all_results: list[dict] = []
            errors: list[str] = []

            for sid in exact_search_ids:
                player = await _search_player_in_season(mode, season_id, sid)
                if player:
                    all_results.append(player)
                else:
                    errors.append(sid)

            for token in raw_fallback_tokens:
                players = await _search_all_players_in_season(mode, season_id, token)
                if players:
                    all_results.extend(players)
                else:
                    errors.append(token)

            # Output
            if len(all_results) == 1 and not errors:
                # Single result — compact format
                player = all_results[0]
                rank = player.get("position", 0)
                score = player.get("score", 0)
                tag = player.get("battle_tag", "")
                display_name = tag.split("#")[0]
                reply = (
                    f"国服{mode_display}{_season_display(season_name)}榜单 - 玩家【{display_name}】\n"
                    f"第{rank}名 - {display_name} - {score}"
                )
                await matcher.finish(reply)
            elif not all_results and errors:
                if len(errors) == 1:
                    await matcher.finish(
                        f"在国服{mode_display}{_season_display(season_name)}中未找到玩家【{errors[0]}】。"
                    )
                else:
                    lines = [f"国服{mode_display}{_season_display(season_name)}榜单"]
                    for err_name in errors:
                        lines.append(f"未找到【{err_name}】的排名记录。")
                    await matcher.finish("\n".join(lines))
            else:
                lines = [f"国服{mode_display}{_season_display(season_name)}榜单"]
                for i, player in enumerate(all_results):
                    rank = player.get("position", 0)
                    name = player.get("battle_tag", "隐藏玩家").split("#")[0]
                    score = player.get("score", 0)
                    lines.append(f"第{rank}名 - {name} - {score}")

                for err_name in errors:
                    lines.append(f"未找到【{err_name}】的排名记录。")

                reply_text = "\n".join(lines)
                await matcher.finish(reply_text)
        else:
            pages_needed = (limit + 24) // 25
            all_players = await _fetch_all_pages(mode, season_id, pages_needed, use_cache, cache_ttl)

            if not all_players:
                await matcher.finish(f"未查询到{_season_display(season_name)}的榜单数据。")

            display = all_players[:limit]

            lines = [f"国服{mode_display}{_season_display(season_name)}榜单 (1-{len(display)}名)"]
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

    if not user_input:
        bindings = _get_bindings_for_qq(event.get_user_id())
        if bindings:
            user_input = bindings[0]["name"]

    season_id, limit, season_name, player_names = parse_args(user_input, is_old=False)

    if not season_id:
        await arena_cmd.finish("未找到对应的赛季参数，请尝试输入如：查竞技场 最新")

    await query_arena(arena_cmd, "undergroundarena", season_id, limit, season_name, player_names, event.get_user_id())


@old_arena_cmd.handle()
async def handle_old_arena_rank(event: MessageEvent, args: Message = CommandArg()):
    user_input = args.extract_plain_text().strip()
    season_id, limit, season_name, player_names = parse_args(user_input, is_old=True)

    if not season_id:
        await old_arena_cmd.finish("未找到对应的赛季参数，旧版竞技场赛季范围为 49~56")

    await query_arena(old_arena_cmd, "arena", season_id, limit, season_name, player_names, event.get_user_id())


@career_cmd.handle()
async def handle_career(event: MessageEvent, args: Message = CommandArg()):
    user_input = args.extract_plain_text().strip()

    if not user_input:
        bindings = _get_bindings_for_qq(event.get_user_id())
        if bindings:
            user_input = bindings[0]["name"]

    if not user_input:
        await career_cmd.finish("请输入玩家ID，如：生涯 玩家名")

    await query_career(career_cmd, user_input)


@tutorial_cmd.handle()
async def handle_tutorial(event: MessageEvent, args: Message = CommandArg()):
    await tutorial_cmd.finish(TUTORIAL_TEXT)


CUTOFF_RANKS = [10, 25, 50, 100, 500]
CUTOFF_PAGES = (max(CUTOFF_RANKS) + 24) // 25  # 20 pages for rank 500


@cutoff_cmd.handle()
async def handle_cutoff(event: MessageEvent, args: Message = CommandArg()):
    user_input = args.extract_plain_text().strip()
    mode = "undergroundarena"
    mode_display = "地下竞技场"
    season_id = SEASON_ALIASES["默认"]  # latest season
    season_name = "最新赛季"

    # Accept optional season parameter
    if user_input:
        parts = user_input.split()
        if parts and parts[0].isdigit():
            num = int(parts[0])
            if 1 <= num <= 6:
                season_id = str(num)
                season_name = str(num)

    try:
        all_players = await _fetch_all_pages(mode, season_id, CUTOFF_PAGES, use_cache=True, ttl=_resolve_cache_ttl(mode, season_id))

        if not all_players:
            await cutoff_cmd.finish(f"未查询到{_season_display(season_name)}的榜单数据。")

        lines = [f"国服{mode_display}{_season_display(season_name)}分数线"]
        for cutoff in CUTOFF_RANKS:
            if cutoff <= len(all_players):
                player = all_players[cutoff - 1]
                name = player.get("battle_tag", "隐藏玩家").split("#")[0]
                score = player.get("score", 0)
                lines.append(f"第{cutoff}名 - {name} - {score}")
            else:
                lines.append(f"[前{cutoff}] 第{cutoff}名 - 数据不足")

        await cutoff_cmd.finish("\n".join(lines))

    except FinishedException:
        raise
    except httpx.TimeoutException:
        await cutoff_cmd.finish("⏳ 请求官方接口超时，请稍后再试。")
    except Exception as e:
        logger.error(f"分数线查询报错: {e}")
        await cutoff_cmd.finish("❌ 查询发生未知错误，请联系管理员。")
