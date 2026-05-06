import json
import threading
from pathlib import Path

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg
from nonebot.log import logger
from nonebot.exception import FinishedException

from arena_bot_cn.plugins.bind_account import _get_bindings_for_qq, _find_binding_by_game_id

DATA_DIR = Path(__file__).parent.parent.parent / "data" / "arena_bot_cn"
DATA_DIR.mkdir(parents=True, exist_ok=True)

_records_cache: dict | None = None
_records_cache_lock = threading.Lock()

MAX_DELETE_INDICES = 30
MAX_WINS = 12
MIN_WINS = 0
EMA_DECAY = 0.9

CLASS_NAMES = [
    "恶魔猎手", "德鲁伊", "猎人", "法师",
    "圣骑士", "牧师", "潜行者", "萨满祭司",
    "术士", "死亡骑士", "战士",
]

# Lowercase alias → canonical class name. Add/remove entries to maintain.
CLASS_ALIASES: dict[str, str] = {
    "dk": "死亡骑士",
    "死骑": "死亡骑士",
    "dh": "恶魔猎手",
    "瞎子": "恶魔猎手",
    "sm": "萨满祭司",
    "萨满": "萨满祭司",
    "fs": "法师",
    "法": "法师",
    "lr": "猎人",
    "猎": "猎人",
    "sq": "圣骑士",
    "骑": "圣骑士",
    "圣骑": "圣骑士",
    "ms": "牧师",
    "牧": "牧师",
    "dz": "潜行者",
    "贼": "潜行者",
    "盗贼": "潜行者",
    "ss": "术士",
    "术": "术士",
    "zs": "战士",
    "战士": "战士",
    "dly": "德鲁伊",
    "德": "德鲁伊",
}


def _resolve_class(name: str) -> str | None:
    """Resolve a class name or alias (case-insensitive) to canonical class name."""
    name_lower = name.lower()
    if name_lower in CLASS_ALIASES:
        return CLASS_ALIASES[name_lower]
    for cls_name in CLASS_NAMES:
        if name_lower == cls_name.lower():
            return cls_name
    return None

upload_cmd = on_command("战绩上传", aliases={"上传战绩", "upload"}, priority=10, block=True)
my_records_cmd = on_command("我的战绩", aliases={"myrecord", "myrecords"}, priority=10, block=True)
delete_records_cmd = on_command("删除战绩", aliases={"deleterecord", "deleterecords"}, priority=10, block=True)
clear_records_cmd = on_command("清空战绩", aliases={"clearrecords"}, priority=10, block=True)


# ============================================================
# Data layer
# ============================================================

def _load_records() -> dict:
    global _records_cache
    with _records_cache_lock:
        if _records_cache is not None:
            return _records_cache

    path = DATA_DIR / "battle_records.json"
    if not path.exists():
        data = {"by_game_id": {}}
    else:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {"by_game_id": {}}

    with _records_cache_lock:
        if _records_cache is None:
            _records_cache = data
        return _records_cache


def _save_records(data: dict) -> None:
    global _records_cache
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / "battle_records.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with _records_cache_lock:
        _records_cache = data


def _recalc_scores(rounds: list[dict]) -> None:
    """Recalculate EMA scores for a list of rounds in-place."""
    if not rounds:
        return
    rounds[0]["score"] = rounds[0]["wins"] * 100
    for i in range(1, len(rounds)):
        rounds[i]["score"] = round(rounds[i - 1]["score"] * EMA_DECAY + rounds[i]["wins"] * 100, 1)


# ============================================================
# Arg parsing
# ============================================================

def _parse_upload_args(user_input: str, qq: str) -> tuple[str | None, str | None, str | None, int | None, int | None, str | None]:
    """Parse upload args (new format). Returns (game_id_lower, display_name, class_name, wins, losses, error).
    Format: 战绩上传 [游戏ID] 职业 胜场 负场"""
    parts = user_input.split()
    if not parts:
        return (None, None, None, None, None,
                "请使用：战绩上传 [游戏ID] 职业 胜场 负场\n示例：战绩上传 小明 法师 12 0")

    # ── Parse wins & losses from the right ──
    if len(parts) < 3:
        return (None, None, None, None, None,
                "请提供职业、胜场数和负场数。\n示例：战绩上传 法师 12 0")

    loss_str = parts[-1]
    win_str = parts[-2]

    if not win_str.isdigit():
        return (None, None, None, None, None, f"胜场参数「{win_str}」不是有效数字。")
    if not loss_str.isdigit():
        return (None, None, None, None, None, f"负场参数「{loss_str}」不是有效数字。")

    wins = int(win_str)
    losses = int(loss_str)

    if wins < MIN_WINS or wins > MAX_WINS:
        return (None, None, None, None, None, f"胜场{wins}超出合法范围（{MIN_WINS}~{MAX_WINS}）。")

    if wins <= 11:
        if losses != 3:
            return (None, None, None, None, None, f"胜场{wins}时负场必须为3，实际为{losses}。")
    elif wins == 12:
        if losses not in (0, 1, 2):
            return (None, None, None, None, None, f"胜场12时负场只能为0/1/2，实际为{losses}。")

    # ── Parse class (third token from right) ──
    class_str = parts[-3]
    class_name = _resolve_class(class_str)
    if class_name is None:
        return (None, None, None, None, None,
                f"「{class_str}」不是合法的职业名称或别名。\n合法职业：{' / '.join(CLASS_NAMES)}")

    # ── Parse game ID (everything before the last 3 tokens) ──
    game_id_str = " ".join(parts[:-3]).strip()

    if not game_id_str:
        bindings = _get_bindings_for_qq(qq)
        if not bindings:
            return (None, None, None, None, None,
                    "你还没有绑定任何游戏账号，请先使用「账号绑定 游戏ID」进行绑定。")
        return (bindings[0]["game_id"], bindings[0]["name"], class_name, wins, losses, None)

    game_id_lower = game_id_str.lower()
    binding = _find_binding_by_game_id(game_id_lower)
    if binding is None:
        return (None, None, None, None, None,
                f"游戏账号【{game_id_str}】未绑定到你的QQ，请先使用「账号绑定 {game_id_str}」进行绑定。")
    if binding["qq"] != qq:
        return (None, None, None, None, None,
                f"游戏账号【{game_id_str}】未绑定到你的QQ，请先使用「账号绑定 {game_id_str}」进行绑定。")

    return (game_id_lower, binding["name"], class_name, wins, losses, None)


def _resolve_game_id(user_input: str, qq: str) -> tuple[str | None, str | None, str | None]:
    """Resolve game_id from input or default to first binding.
    Returns (game_id_lower, display_name, error_message)."""
    user_input = user_input.strip()

    if not user_input:
        bindings = _get_bindings_for_qq(qq)
        if not bindings:
            return (None, None, "你还没有绑定任何游戏账号，请先使用「账号绑定 游戏ID」进行绑定。")
        return (bindings[0]["game_id"], bindings[0]["name"], None)

    game_id_lower = user_input.lower()
    binding = _find_binding_by_game_id(game_id_lower)
    if binding is None:
        return (None, None, f"游戏账号【{user_input}】未找到绑定记录。")
    if binding["qq"] != qq:
        return (None, None, f"游戏账号【{user_input}】不在你的绑定列表中。")

    return (game_id_lower, binding["name"], None)


def _parse_delete_args(user_input: str, qq: str) -> tuple[str | None, str | None, list[int] | None, str | None]:
    """Parse delete args. Returns (game_id_lower, display_name, indices, error_message).
    Indices are 1-based from user input, returned as-is for deletion logic."""
    parts = user_input.split()
    if not parts:
        game_id_lower, display_name, error = _resolve_game_id("", qq)
        if error:
            return (None, None, None, error)
        return (game_id_lower, display_name, None, "请指定要删除的轮次索引。\n示例：删除战绩 小明 1 3 5")

    # First token is game_id if non-numeric; otherwise all tokens are indices
    first = parts[0]
    if first.isdigit():
        # All tokens are indices, game_id defaults to first binding
        game_id_lower, display_name, error = _resolve_game_id("", qq)
        if error:
            return (None, None, None, error)
        index_tokens = parts
    else:
        game_id_lower = first.lower()
        binding = _find_binding_by_game_id(game_id_lower)
        if binding is None:
            return (None, None, None, f"游戏账号【{first}】未找到绑定记录。")
        if binding["qq"] != qq:
            return (None, None, None, f"游戏账号【{first}】不在你的绑定列表中。")
        display_name = binding["name"]
        index_tokens = parts[1:]

    if not index_tokens:
        return (None, None, None, "请指定要删除的轮次索引。\n示例：删除战绩 小明 1 3 5")

    if len(index_tokens) > MAX_DELETE_INDICES:
        return (None, None, None, f"单次最多删除 {MAX_DELETE_INDICES} 个轮次。")

    indices = []
    for i, s in enumerate(index_tokens):
        if not s.isdigit():
            return (None, None, None, f"轮次索引「{s}」不是有效数字。")
        idx = int(s)
        if idx < 1:
            return (None, None, None, f"轮次索引「{idx}」无效，索引从 1 开始。")
        indices.append(idx)

    return (game_id_lower, display_name, indices, None)


# ============================================================
# Command handlers
# ============================================================

async def _handle_upload(matcher, qq: str, user_input: str):
    game_id_lower, display_name, class_name, wins, losses, error = _parse_upload_args(user_input, qq)
    if error:
        await matcher.finish(error)
        return

    data = _load_records()
    entry = data.get("by_game_id", {}).get(game_id_lower)
    if entry is None:
        entry = {"rounds": []}
        if "by_game_id" not in data:
            data["by_game_id"] = {}
        data["by_game_id"][game_id_lower] = entry

    entry["rounds"].append({"wins": wins, "losses": losses, "class": class_name, "score": 0})
    _recalc_scores(entry["rounds"])
    _save_records(data)

    total_rounds = len(entry["rounds"])
    latest_score = entry["rounds"][-1]["score"]
    await matcher.finish(
        f"上传成功！{class_name} {wins}胜{losses}负已记录。\n"
        f"当前总轮次: {total_rounds}\n"
        f"当前分数: {latest_score}"
    )


async def _handle_my_records(matcher, qq: str, user_input: str):
    game_id_lower, display_name, error = _resolve_game_id(user_input, qq)
    if error:
        await matcher.finish(error)
        return

    data = _load_records()
    entry = data.get("by_game_id", {}).get(game_id_lower)

    if entry is None or not entry.get("rounds"):
        await matcher.finish(f"游戏账号【{display_name}】还没有战绩记录。\n使用「战绩上传 [游戏ID] 职业 胜场 负场」上传战绩。")
        return

    rounds = entry["rounds"]
    lines = [f"游戏账号【{display_name}】的战绩记录（共{len(rounds)}轮）"]
    for i, r in enumerate(rounds, start=1):
        c = r.get("class", "-")
        w = r["wins"]
        lo = r.get("losses", "-")
        lines.append(f"第{i}轮. {c} {w}胜{lo}负")
    lines.append(f"当前分数: {rounds[-1]['score']}")

    await matcher.finish("\n".join(lines))


async def _handle_delete(matcher, qq: str, user_input: str):
    game_id_lower, display_name, indices, error = _parse_delete_args(user_input, qq)
    if error:
        await matcher.finish(error)
        return

    data = _load_records()
    entry = data.get("by_game_id", {}).get(game_id_lower)

    if entry is None or not entry.get("rounds"):
        await matcher.finish(f"游戏账号【{display_name}】还没有战绩记录，无法删除。")
        return

    rounds = entry["rounds"]
    max_idx = len(rounds)

    # Validate indices
    invalid = [idx for idx in indices if idx > max_idx]
    if invalid:
        await matcher.finish(f"轮次索引 {invalid} 超出范围（当前共{max_idx}轮）。")
        return

    # Delete in descending order to avoid index shift
    for idx in sorted(set(indices), reverse=True):
        del rounds[idx - 1]  # convert 1-based to 0-based

    _recalc_scores(rounds)
    _save_records(data)

    await matcher.finish(
        f"已删除游戏账号【{display_name}】的 {len(set(indices))} 轮战绩。\n"
        f"剩余轮次: {len(rounds)}"
    )


async def _handle_clear(matcher, qq: str, user_input: str):
    game_id_lower, display_name, error = _resolve_game_id(user_input, qq)
    if error:
        await matcher.finish(error)
        return

    data = _load_records()
    entry = data.get("by_game_id", {}).get(game_id_lower)

    if entry is None or not entry.get("rounds"):
        await matcher.finish(f"游戏账号【{display_name}】还没有战绩记录，无需清空。")
        return

    count = len(entry["rounds"])
    del data["by_game_id"][game_id_lower]
    _save_records(data)

    await matcher.finish(f"已清空游戏账号【{display_name}】的全部 {count} 轮战绩记录。")


# ============================================================
# Command registration
# ============================================================

@upload_cmd.handle()
async def handle_upload(event: MessageEvent, args: Message = CommandArg()):
    user_input = args.extract_plain_text().strip()
    qq = event.get_user_id()
    try:
        await _handle_upload(upload_cmd, qq, user_input)
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"战绩上传操作报错: {e}")
        await upload_cmd.finish("上传战绩发生未知错误，请联系管理员。")


@my_records_cmd.handle()
async def handle_my_records(event: MessageEvent, args: Message = CommandArg()):
    user_input = args.extract_plain_text().strip()
    qq = event.get_user_id()
    try:
        await _handle_my_records(my_records_cmd, qq, user_input)
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"我的战绩操作报错: {e}")
        await my_records_cmd.finish("查询战绩发生未知错误，请联系管理员。")


@delete_records_cmd.handle()
async def handle_delete_records(event: MessageEvent, args: Message = CommandArg()):
    user_input = args.extract_plain_text().strip()
    qq = event.get_user_id()
    try:
        await _handle_delete(delete_records_cmd, qq, user_input)
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"删除战绩操作报错: {e}")
        await delete_records_cmd.finish("删除战绩发生未知错误，请联系管理员。")


@clear_records_cmd.handle()
async def handle_clear_records(event: MessageEvent, args: Message = CommandArg()):
    user_input = args.extract_plain_text().strip()
    qq = event.get_user_id()
    try:
        await _handle_clear(clear_records_cmd, qq, user_input)
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"清空战绩操作报错: {e}")
        await clear_records_cmd.finish("清空战绩发生未知错误，请联系管理员。")
