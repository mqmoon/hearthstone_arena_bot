import asyncio
import io
import json
import tempfile
import threading
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg
from nonebot.log import logger
from nonebot.exception import FinishedException

from arena_bot_cn.plugins.bind_account import _get_bindings_for_qq, _find_binding_by_game_id

DATA_DIR = Path(__file__).parent.parent.parent / "data" / "arena_bot_cn"
DATA_DIR.mkdir(parents=True, exist_ok=True)

_records_cache: dict | None = None
_records_cache_lock = threading.Lock()

MAX_DELETE_INDICES = 30
MAX_MODIFY_ROUNDS = 30
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
    "瞎": "恶魔猎手",
    "sm": "萨满祭司",
    "萨满": "萨满祭司",
    "萨": "萨满祭司",
    "fs": "法师",
    "法": "法师",
    "lr": "猎人",
    "猎": "猎人",
    "sq": "圣骑士",
    "骑": "圣骑士",
    "圣骑": "圣骑士",
    "骑士": "圣骑士",
    "ms": "牧师",
    "牧": "牧师",
    "dz": "潜行者",
    "贼": "潜行者",
    "盗贼": "潜行者",
    "ss": "术士",
    "术": "术士",
    "zs": "战士",
    "战": "战士",
    "dly": "德鲁伊",
    "德": "德鲁伊",
    "小德": "德鲁伊",
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
record_list_cmd = on_command("战绩列表", aliases={"我的战绩", "recordlist", "盒战绩", "战绩查询"}, priority=10, block=True)
delete_records_cmd = on_command("删除战绩", aliases={"deleterecord", "deleterecords"}, priority=10, block=True)
modify_records_cmd = on_command("修改战绩", aliases={"战绩修改", "editrecord", "editrecords", "modifyrecord"}, priority=10, block=True)
clear_records_cmd = on_command("清空战绩", aliases={"clearrecords"}, priority=10, block=True)
class_dist_cmd = on_command("战绩分布", aliases={"classdist"}, priority=10, block=True)
score_query_cmd = on_command("分数查询", aliases={"score", "盒分数"}, priority=10, block=True)
pk_cmd = on_command("与菜逼PK", priority=10, block=True)
any_pk_cmd = on_command("PK", aliases={"pk", "任意PK", "anypk"}, priority=10, block=True)
win_dist_cmd = on_command("胜场分布", aliases={"windist"}, priority=10, block=True)


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
    """Recalculate EMA scores and avg_wins for a list of rounds in-place."""
    if not rounds:
        return
    # EMA score
    rounds[0]["score"] = rounds[0]["wins"] * 100
    for i in range(1, len(rounds)):
        rounds[i]["score"] = round(rounds[i - 1]["score"] * EMA_DECAY + rounds[i]["wins"] * 100, 1)

    # Avg wins (旧版竞技场算法): simple mean for ≤30 rounds, EMA(1/30) after
    running_sum = 0
    for i, r in enumerate(rounds):
        running_sum += r["wins"]
        if i < 30:
            r["avg_wins"] = round(running_sum / (i + 1), 2)
        else:
            r["avg_wins"] = round(rounds[i - 1]["avg_wins"] * 29 / 30 + r["wins"] / 30, 2)


# ============================================================
# Arg parsing
# ============================================================

def _parse_upload_args(user_input: str, qq: str) -> tuple[str | None, str | None, list[dict] | None, str | None]:
    """Parse multi-round upload args. Returns (game_id_lower, display_name, rounds, error).
    rounds is a list of {"class": str, "wins": int, "losses": int} dicts.
    Format: 战绩上传 [游戏ID] 职业 胜场 [负场] [职业 胜场 [负场] ...]
    胜场0-11时负场可省略（默认负场=3），胜场12时负场必填（0/1/2）。
    单次最多上传30轮。"""
    parts = user_input.split()
    if not parts:
        return (None, None, None,
                "请使用：战绩上传 [游戏ID] 职业 胜场 [负场] [职业 胜场 [负场] ...]\n"
                "胜场0-11时可省略负场（默认负场=3），单次最多30轮\n"
                "示例：战绩上传 法师 7\n"
                "示例：战绩上传 法 7 牧 12 1 猎 5\n"
                "示例：战绩上传 小明 法师 12 0")

    # Find game_id boundary: scan left-to-right for first <class-like> <digit> pair
    game_id_end = 0
    for i in range(len(parts) - 1):
        if _resolve_class(parts[i]) is not None and parts[i + 1].isdigit():
            game_id_end = i
            break
    else:
        return (None, None, None,
                "无法识别职业/胜场，请检查输入格式。\n"
                "格式：战绩上传 [游戏ID] 职业 胜场 [负场] [...]")

    game_id_str = " ".join(parts[:game_id_end]).strip()
    round_tokens = parts[game_id_end:]

    # Resolve game_id
    if not game_id_str:
        bindings = _get_bindings_for_qq(qq)
        if not bindings:
            return (None, None, None,
                    "你还没有绑定任何游戏账号，请先使用「账号绑定 游戏ID」进行绑定。")
        game_id_lower = bindings[0]["game_id"]
        display_name = bindings[0]["name"]
    else:
        game_id_lower = game_id_str.lower()
        binding = _find_binding_by_game_id(game_id_lower)
        if binding is None:
            return (None, None, None,
                    f"游戏账号【{game_id_str}】未绑定到你的QQ，请先使用「账号绑定 {game_id_str}」进行绑定。")
        if binding["qq"] != qq:
            return (None, None, None,
                    f"游戏账号【{game_id_str}】未绑定到你的QQ，请先使用「账号绑定 {game_id_str}」进行绑定。")
        display_name = binding["name"]

    # Parse rounds greedily
    rounds: list[dict] = []
    idx = 0
    while idx < len(round_tokens):
        round_num = len(rounds) + 1
        class_str = round_tokens[idx]
        class_name = _resolve_class(class_str)
        if class_name is None:
            return (None, None, None,
                    f"第{round_num}轮：「{class_str}」不是合法的职业名称或别名。\n"
                    f"合法职业：{' / '.join(CLASS_NAMES)}")

        if idx + 1 >= len(round_tokens):
            return (None, None, None, f"第{round_num}轮：缺少胜场参数。")

        win_str = round_tokens[idx + 1]
        if not win_str.isdigit():
            return (None, None, None, f"第{round_num}轮：胜场「{win_str}」不是有效数字。")
        wins = int(win_str)

        if wins < MIN_WINS or wins > MAX_WINS:
            return (None, None, None,
                    f"第{round_num}轮：胜场{wins}超出合法范围（{MIN_WINS}~{MAX_WINS}）。")

        if wins == 12:
            if idx + 2 >= len(round_tokens):
                return (None, None, None,
                        f"第{round_num}轮：胜场12时必须提供负场（0/1/2）。")
            loss_str = round_tokens[idx + 2]
            if not loss_str.isdigit():
                return (None, None, None,
                        f"第{round_num}轮：负场「{loss_str}」不是有效数字。")
            losses = int(loss_str)
            if losses not in (0, 1, 2):
                return (None, None, None,
                        f"第{round_num}轮：胜场12时负场只能为0/1/2，实际为{losses}。")
            idx += 3
        else:
            # wins 0-11: losses optional, defaults to 3
            if idx + 2 < len(round_tokens) and round_tokens[idx + 2].isdigit():
                losses = int(round_tokens[idx + 2])
                if losses != 3:
                    return (None, None, None,
                            f"第{round_num}轮：胜场{wins}时负场必须为3，实际为{losses}。\n"
                            f"提示：胜场0-11时可省略负场，默认负场=3。")
                idx += 3
            else:
                losses = 3
                idx += 2

        rounds.append({"class": class_name, "wins": wins, "losses": losses})

        if len(rounds) > 30:
            return (None, None, None, "单次最多上传30轮战绩。")

    if not rounds:
        return (None, None, None, "未能解析任何有效轮次。")

    return (game_id_lower, display_name, rounds, None)


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


def _resolve_game_id_lookup(user_input: str, qq: str, raw_message: Message | None = None) -> tuple[str | None, str | None, str | None]:
    """Resolve game_id for lookup/query commands. No binding restriction when game_id is provided.
    Supports @mention: if the message contains an at segment, use the mentioned QQ's first binding.
    Returns (game_id_lower, display_name, error_message)."""
    # Check for @mention first — extract the first at segment's QQ
    if raw_message is not None:
        for seg in raw_message:
            if seg.type == "at":
                at_qq = seg.data.get("qq")
                if at_qq:
                    bindings = _get_bindings_for_qq(at_qq)
                    if not bindings:
                        return (None, None, f"被@的用户还没有绑定任何游戏账号，请先让对方使用「账号绑定 游戏ID」进行绑定。")
                    return (bindings[0]["game_id"], bindings[0]["name"], None)
                break

    user_input = user_input.strip()

    if not user_input:
        bindings = _get_bindings_for_qq(qq)
        if not bindings:
            return (None, None, "你还没有绑定任何游戏账号，请先使用「账号绑定 游戏ID」进行绑定。")
        return (bindings[0]["game_id"], bindings[0]["name"], None)

    game_id_lower = user_input.lower()
    return (game_id_lower, user_input, None)


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


def _parse_modify_args(user_input: str, qq: str) -> tuple[str | None, str | None, list[dict] | None, str | None]:
    """Parse modify args. Returns (game_id_lower, display_name, edits, error_message).
    Edits are {"index", "class", "wins", "losses"} dicts; index is 1-based as entered.
    Format: 修改战绩 [游戏ID] 轮次 职业 胜场 [负场] [轮次 职业 胜场 [负场] ...]
    胜场0-11时负场固定为3，无需填写；胜场12时必须填写负场（0/1/2）。
    单次最多修改30轮，同一轮次不可重复指定。"""
    parts = user_input.split()
    if not parts:
        game_id_lower, display_name, error = _resolve_game_id("", qq)
        if error:
            return (None, None, None, error)
        return (game_id_lower, display_name, None,
                "请指定要修改的轮次及新战绩。\n示例：修改战绩 小明 1 法师 7\n"
                "示例：修改战绩 1 法 7 2 牧 12 1")

    # First token is game_id if non-numeric; otherwise it is the first round index
    first = parts[0]
    if first.isdigit():
        game_id_lower, display_name, error = _resolve_game_id("", qq)
        if error:
            return (None, None, None, error)
        edit_tokens = parts
    else:
        game_id_lower = first.lower()
        binding = _find_binding_by_game_id(game_id_lower)
        if binding is None:
            return (None, None, None, f"游戏账号【{first}】未找到绑定记录。")
        if binding["qq"] != qq:
            return (None, None, None, f"游戏账号【{first}】不在你的绑定列表中。")
        display_name = binding["name"]
        edit_tokens = parts[1:]

    if not edit_tokens:
        return (None, None, None,
                "请指定要修改的轮次及新战绩。\n示例：修改战绩 小明 1 法师 7")

    edits: list[dict] = []
    seen_indices: set[int] = set()
    idx = 0
    while idx < len(edit_tokens):
        edit_num = len(edits) + 1
        index_str = edit_tokens[idx]
        if not index_str.isdigit():
            return (None, None, None, f"第{edit_num}处修改：轮次索引「{index_str}」不是有效数字。")
        round_index = int(index_str)
        if round_index < 1:
            return (None, None, None, f"第{edit_num}处修改：轮次索引「{round_index}」无效，索引从 1 开始。")
        if round_index in seen_indices:
            return (None, None, None, f"第{edit_num}处修改：轮次索引「{round_index}」重复指定。")
        seen_indices.add(round_index)

        if idx + 1 >= len(edit_tokens):
            return (None, None, None, f"第{edit_num}处修改：轮次{round_index}缺少职业参数。")
        class_str = edit_tokens[idx + 1]
        class_name = _resolve_class(class_str)
        if class_name is None:
            return (None, None, None,
                    f"第{edit_num}处修改：「{class_str}」不是合法的职业名称或别名。\n"
                    f"合法职业：{' / '.join(CLASS_NAMES)}")

        if idx + 2 >= len(edit_tokens):
            return (None, None, None, f"第{edit_num}处修改：轮次{round_index}缺少胜场参数。")
        win_str = edit_tokens[idx + 2]
        if not win_str.isdigit():
            return (None, None, None, f"第{edit_num}处修改：胜场「{win_str}」不是有效数字。")
        wins = int(win_str)
        if wins < MIN_WINS or wins > MAX_WINS:
            return (None, None, None,
                    f"第{edit_num}处修改：胜场{wins}超出合法范围（{MIN_WINS}~{MAX_WINS}）。")

        if wins == 12:
            if idx + 3 >= len(edit_tokens):
                return (None, None, None,
                        f"第{edit_num}处修改：轮次{round_index}胜场12时必须提供负场（0/1/2）。")
            loss_str = edit_tokens[idx + 3]
            if not loss_str.isdigit():
                return (None, None, None,
                        f"第{edit_num}处修改：负场「{loss_str}」不是有效数字。")
            losses = int(loss_str)
            if losses not in (0, 1, 2):
                return (None, None, None,
                        f"第{edit_num}处修改：胜场12时负场只能为0/1/2，实际为{losses}。")
            idx += 4
        else:
            # wins 0-11: losses is always 3 and cannot be specified separately
            losses = 3
            idx += 3

        edits.append({"index": round_index, "class": class_name, "wins": wins, "losses": losses})

        if len(edits) > MAX_MODIFY_ROUNDS:
            return (None, None, None, f"单次最多修改 {MAX_MODIFY_ROUNDS} 轮战绩。")

    return (game_id_lower, display_name, edits, None)


# ============================================================
# Command handlers
# ============================================================

async def _handle_upload(matcher, qq: str, user_input: str):
    game_id_lower, display_name, rounds, error = _parse_upload_args(user_input, qq)
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

    for r in rounds:
        entry["rounds"].append({"wins": r["wins"], "losses": r["losses"], "class": r["class"], "score": 0})
    _recalc_scores(entry["rounds"])
    _save_records(data)

    total_rounds = len(entry["rounds"])
    latest = entry["rounds"][-1]
    latest_score = latest["score"]
    latest_avg = latest.get("avg_wins", "-")
    uploaded_count = len(rounds)
    await matcher.finish(
        f"上传成功！已记录{uploaded_count}轮战绩。\n"
        f"当前总轮次: {total_rounds}\n"
        f"当前分数: {latest_score}  均胜: {latest_avg}"
    )


async def _handle_record_list(matcher, qq: str, user_input: str, raw_message: Message | None = None):
    game_id_lower, display_name, error = _resolve_game_id_lookup(user_input, qq, raw_message)
    if error:
        await matcher.finish(error)
        return

    data = _load_records()
    entry = data.get("by_game_id", {}).get(game_id_lower)

    if entry is None or not entry.get("rounds"):
        await matcher.finish(f"游戏账号【{display_name}】还没有战绩记录。")
        return

    rounds = entry["rounds"]
    lines = [f"游戏账号【{display_name}】的战绩记录（共{len(rounds)}轮）"]
    for i, r in enumerate(rounds, start=1):
        c = r.get("class", "-")
        w = r["wins"]
        lo = r.get("losses", "-")
        lines.append(f"第{i}轮. {c} {w}胜{lo}负")
    latest = rounds[-1]
    lines.append(f"当前分数: {latest['score']}  均胜: {latest.get('avg_wins', '-')}")

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


async def _handle_modify(matcher, qq: str, user_input: str):
    game_id_lower, display_name, edits, error = _parse_modify_args(user_input, qq)
    if error:
        await matcher.finish(error)
        return

    data = _load_records()
    entry = data.get("by_game_id", {}).get(game_id_lower)

    if entry is None or not entry.get("rounds"):
        await matcher.finish(f"游戏账号【{display_name}】还没有战绩记录，无法修改。")
        return

    rounds = entry["rounds"]
    max_idx = len(rounds)

    # Validate indices
    invalid = sorted({e["index"] for e in edits if e["index"] > max_idx})
    if invalid:
        await matcher.finish(f"轮次索引 {invalid} 超出范围（当前共{max_idx}轮）。")
        return

    for e in edits:
        r = rounds[e["index"] - 1]  # convert 1-based to 0-based
        r["class"] = e["class"]
        r["wins"] = e["wins"]
        r["losses"] = e["losses"]

    # Recalculate after mutation so score/avg_wins and every derived view stay consistent
    _recalc_scores(rounds)
    _save_records(data)

    latest = rounds[-1]
    lines = [f"已修改游戏账号【{display_name}】的 {len(edits)} 轮战绩。"]
    for e in edits:
        lines.append(f"第{e['index']}轮. {e['class']} {e['wins']}胜{e['losses']}负")
    lines.append(f"当前轮次: {len(rounds)}")
    lines.append(f"当前分数: {latest['score']}  均胜: {latest.get('avg_wins', '-')}")

    await matcher.finish("\n".join(lines))


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


def _find_cjk_font() -> str | None:
    """Find a CJK-capable font available on this system."""
    # 1. Hardcoded common paths
    candidates = [
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "C:\\Windows\\Fonts\\msyh.ttc",
        "C:\\Windows\\Fonts\\simhei.ttf",
        "C:\\Windows\\Fonts\\simsun.ttc",
    ]
    for path in candidates:
        if Path(path).exists():
            return path

    # 2. Search matplotlib's own font list for CJK fonts
    cjk_keywords = ["CJK", "WenQuanYi", "Noto Sans", "SimHei", "SimSun", "Microsoft YaHei"]
    for f in font_manager.fontManager.ttflist:
        for kw in cjk_keywords:
            if kw.lower() in f.name.lower():
                return f.fname

    return None

CHART_FONT_PATH = _find_cjk_font()


def _generate_distribution_chart(rounds: list[dict], game_name: str, score: float, avg_wins: float) -> Path:
    """Generate class distribution bar chart. Returns path to saved PNG."""
    class_stats: dict[str, list[int]] = {}
    for r in rounds:
        cls = r.get("class", "未知")
        class_stats.setdefault(cls, []).append(r["wins"])

    total = len(rounds)
    # Build (label, pure_avg_wins) sorted by avg_wins desc
    entries = []
    for cls, wins_list in class_stats.items():
        count = len(wins_list)
        pct = count / total * 100
        avg = sum(wins_list) / count
        entries.append((cls, count, pct, avg))
    entries.sort(key=lambda x: x[3], reverse=True)

    labels = [f"{cls}\n{count}次 {pct:.1f}%" for cls, count, pct, _ in entries]
    values = [avg for _, _, _, avg in entries]

    font_prop = font_manager.FontProperties(fname=CHART_FONT_PATH, size=10)
    title_font = font_manager.FontProperties(fname=CHART_FONT_PATH, size=13)

    fig, ax = plt.subplots(figsize=(max(6, len(entries) * 1.4), 6))
    bars = ax.bar(range(len(entries)), values, color="#4C72B0", width=0.6)
    ax.set_xticks(range(len(entries)))
    ax.set_xticklabels(labels, fontproperties=font_prop)
    ax.set_ylabel("纯均胜", fontproperties=font_prop)
    ax.set_ylim(0, max(values) + 1.5 if values else 5)

    # Value labels on top of bars
    for bar, val in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.2,
                f"{val:.1f}", ha="center", va="bottom",
                fontproperties=font_prop)

    ax.set_title(
        f"{game_name}  战绩分布\n分数: {score}  均胜: {avg_wins}",
        fontproperties=title_font,
        pad=18,
    )
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    fig.savefig(tmp.name, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return Path(tmp.name)


def _generate_win_distribution_chart(rounds: list[dict], game_name: str, score: float, avg_wins: float) -> Path:
    """Generate win-count distribution bar chart. Returns path to saved PNG."""
    win_counts = {i: 0 for i in range(0, 13)}
    for r in rounds:
        w = r["wins"]
        if 0 <= w <= 12:
            win_counts[w] += 1

    labels = [
        f"{w}胜\n{win_counts[w]}次" if win_counts[w] > 0 else f"{w}胜"
        for w in range(0, 13)
    ]
    values = [win_counts[w] for w in range(0, 13)]

    font_prop = font_manager.FontProperties(fname=CHART_FONT_PATH, size=10)
    title_font = font_manager.FontProperties(fname=CHART_FONT_PATH, size=13)

    fig, ax = plt.subplots(figsize=(12, 6))
    bars = ax.bar(range(13), values, color="#4C72B0", width=0.6)
    ax.set_xticks(range(13))
    ax.set_xticklabels(labels, fontproperties=font_prop)
    ax.set_ylabel("次数", fontproperties=font_prop)
    ymax = max(values) + 1 if values and max(values) > 0 else 5
    ax.set_ylim(0, ymax)

    for bar, val in zip(bars, values):
        if val > 0:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.2,
                    str(val), ha="center", va="bottom", fontproperties=font_prop)

    ax.set_title(
        f"{game_name}  胜场分布\n分数: {score}  均胜: {avg_wins}",
        fontproperties=title_font,
        pad=18,
    )
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    fig.savefig(tmp.name, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return Path(tmp.name)


async def _handle_class_distribution(matcher, qq: str, user_input: str, raw_message: Message | None = None):
    if CHART_FONT_PATH is None:
        await matcher.finish("系统未找到中文字体，无法生成柱状图。请联系管理员安装中文字体。")
        return

    game_id_lower, display_name, error = _resolve_game_id_lookup(user_input, qq, raw_message)
    if error:
        await matcher.finish(error)
        return

    data = _load_records()
    entry = data.get("by_game_id", {}).get(game_id_lower)

    if entry is None or not entry.get("rounds"):
        await matcher.finish(f"游戏账号【{display_name}】还没有战绩记录。")
        return

    rounds = entry["rounds"]
    latest = rounds[-1]
    score = latest["score"]
    avg = latest.get("avg_wins", 0)

    chart_path = await asyncio.to_thread(
        _generate_distribution_chart, rounds, display_name, score, avg
    )

    await matcher.finish(MessageSegment.image(file=chart_path))


async def _handle_win_distribution(matcher, qq: str, user_input: str, raw_message: Message | None = None):
    if CHART_FONT_PATH is None:
        await matcher.finish("系统未找到中文字体，无法生成柱状图。请联系管理员安装中文字体。")
        return

    game_id_lower, display_name, error = _resolve_game_id_lookup(user_input, qq, raw_message)
    if error:
        await matcher.finish(error)
        return

    data = _load_records()
    entry = data.get("by_game_id", {}).get(game_id_lower)

    if entry is None or not entry.get("rounds"):
        await matcher.finish(f"游戏账号【{display_name}】还没有战绩记录。")
        return

    rounds = entry["rounds"]
    latest = rounds[-1]
    score = latest["score"]
    avg = latest.get("avg_wins", 0)

    chart_path = await asyncio.to_thread(
        _generate_win_distribution_chart, rounds, display_name, score, avg
    )

    await matcher.finish(MessageSegment.image(file=chart_path))


async def _handle_score_query(matcher, qq: str, user_input: str, raw_message: Message | None = None):
    game_id_lower, display_name, error = _resolve_game_id_lookup(user_input, qq, raw_message)
    if error:
        await matcher.finish(error)
        return

    data = _load_records()
    entry = data.get("by_game_id", {}).get(game_id_lower)

    if entry is None or not entry.get("rounds"):
        await matcher.finish(f"游戏账号【{display_name}】还没有战绩记录。")
        return

    total_rounds = len(entry["rounds"])
    latest = entry["rounds"][-1]
    await matcher.finish(
        f"游戏账号【{display_name}】\n"
        f"当前轮次: {total_rounds}\n"
        f"当前分数: {latest['score']}  均胜: {latest.get('avg_wins', '-')}"
    )


PK_BENCHMARK = "敲可爱丨谜亚星"


async def _handle_pk(matcher, qq: str, user_input: str, raw_message: Message | None = None):
    game_id_lower, display_name, error = _resolve_game_id_lookup(user_input, qq, raw_message)
    if error:
        await matcher.finish(error)
        return

    data = _load_records()

    bench_entry = data.get("by_game_id", {}).get(PK_BENCHMARK.lower())
    if bench_entry is None or not bench_entry.get("rounds"):
        await matcher.finish(f"菜逼【{PK_BENCHMARK}】还没有战绩记录，PK无法进行。")
        return

    player_entry = data.get("by_game_id", {}).get(game_id_lower)
    if player_entry is None or not player_entry.get("rounds"):
        await matcher.finish(f"【{display_name}】还没有战绩记录，PK无法进行。")
        return

    b_rounds = len(bench_entry["rounds"])
    b_latest = bench_entry["rounds"][-1]
    b_score = b_latest["score"]
    b_avg = b_latest.get("avg_wins", "-")

    p_rounds = len(player_entry["rounds"])
    p_latest = player_entry["rounds"][-1]
    p_score = p_latest["score"]
    p_avg = p_latest.get("avg_wins", "-")

    lines = [
        "【与菜逼PK】",
        f"菜逼【{PK_BENCHMARK}】: {b_rounds}轮  分数: {b_score}  均胜: {b_avg}",
        f"【{display_name}】: {p_rounds}轮  分数: {p_score}  均胜: {p_avg}",
    ]

    if p_score > b_score:
        lines.append("你已经击败了菜逼！")
    else:
        needed = (b_score - p_score * EMA_DECAY) / 100
        min_wins = int(needed) + 1 if needed >= 0 else 1
        lines.append(f"只要下一轮取得{min_wins}胜及以上，即可击败菜逼！")

    await matcher.finish("\n".join(lines))


def _resolve_pk_player(token: str) -> tuple[str, str]:
    """Resolve a PK participant token to (game_id_lower, display_name)."""
    token = token.strip()
    game_id_lower = token.lower()
    binding = _find_binding_by_game_id(game_id_lower)
    display_name = binding.get("name", token) if binding else token
    return game_id_lower, display_name


async def _handle_any_pk(matcher, qq: str, user_input: str):
    """PK A B / PK B — 以 A 追赶 B 的视角比较双方最新赛季（最新一轮）成绩。"""
    parts = user_input.split()
    usage = (
        "PK 用法：PK A B（A追赶B的最新赛季成绩）\n"
        "或：PK B（以你的第一个绑定账号作为A）"
    )
    if not parts or len(parts) > 2:
        await matcher.finish(usage)
        return

    if len(parts) == 1:
        bindings = _get_bindings_for_qq(qq)
        if not bindings:
            await matcher.finish("你还没有绑定任何游戏账号，请先使用「账号绑定 游戏ID」进行绑定。")
            return
        a_id, a_name = bindings[0]["game_id"], bindings[0]["name"]
        b_id, b_name = _resolve_pk_player(parts[0])
    else:
        a_id, a_name = _resolve_pk_player(parts[0])
        b_id, b_name = _resolve_pk_player(parts[1])

    if a_id == b_id:
        await matcher.finish(f"【{a_name}】与【{b_name}】是同一个账号，无需PK。")
        return

    entries = _load_records().get("by_game_id", {})

    a_entry = entries.get(a_id)
    if a_entry is None or not a_entry.get("rounds"):
        await matcher.finish(f"【{a_name}】还没有战绩记录，PK无法进行。")
        return

    b_entry = entries.get(b_id)
    if b_entry is None or not b_entry.get("rounds"):
        await matcher.finish(f"【{b_name}】还没有战绩记录，PK无法进行。")
        return

    a_rounds = a_entry["rounds"]
    b_rounds = b_entry["rounds"]
    a_latest = a_rounds[-1]
    b_latest = b_rounds[-1]
    a_score = a_latest.get("score", 0)
    b_score = b_latest.get("score", 0)

    lines = [
        f"【与{b_name}PK】",
        f"【{b_name}】: {len(b_rounds)}轮  分数: {b_score}  均胜: {b_latest.get('avg_wins', '-')}",
        f"【{a_name}】: {len(a_rounds)}轮  分数: {a_score}  均胜: {a_latest.get('avg_wins', '-')}",
    ]

    if a_score > b_score:
        lines.append(f"你已经击败了【{b_name}】！")
    else:
        needed = (b_score - a_score * EMA_DECAY) / 100
        min_wins = int(needed) + 1 if needed >= 0 else 1
        lines.append(f"只要下一轮取得{min_wins}胜及以上，即可击败【{b_name}】！")

    await matcher.finish("\n".join(lines))


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


@record_list_cmd.handle()
async def handle_record_list(event: MessageEvent, args: Message = CommandArg()):
    user_input = args.extract_plain_text().strip()
    qq = event.get_user_id()
    try:
        await _handle_record_list(record_list_cmd, qq, user_input, args)
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"战绩列表操作报错: {e}")
        await record_list_cmd.finish("查询战绩列表发生未知错误，请联系管理员。")


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


@modify_records_cmd.handle()
async def handle_modify_records(event: MessageEvent, args: Message = CommandArg()):
    user_input = args.extract_plain_text().strip()
    qq = event.get_user_id()
    try:
        await _handle_modify(modify_records_cmd, qq, user_input)
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"修改战绩操作报错: {e}")
        await modify_records_cmd.finish("修改战绩发生未知错误，请联系管理员。")


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


@class_dist_cmd.handle()
async def handle_class_distribution(event: MessageEvent, args: Message = CommandArg()):
    user_input = args.extract_plain_text().strip()
    qq = event.get_user_id()
    try:
        await _handle_class_distribution(class_dist_cmd, qq, user_input, args)
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"战绩分布操作报错: {e}")
        await class_dist_cmd.finish("战绩分布生成发生未知错误，请联系管理员。")


@score_query_cmd.handle()
async def handle_score_query(event: MessageEvent, args: Message = CommandArg()):
    user_input = args.extract_plain_text().strip()
    qq = event.get_user_id()
    try:
        await _handle_score_query(score_query_cmd, qq, user_input, args)
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"分数查询操作报错: {e}")
        await score_query_cmd.finish("分数查询发生未知错误，请联系管理员。")


@pk_cmd.handle()
async def handle_pk(event: MessageEvent, args: Message = CommandArg()):
    user_input = args.extract_plain_text().strip()
    qq = event.get_user_id()
    try:
        await _handle_pk(pk_cmd, qq, user_input, args)
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"与菜逼PK操作报错: {e}")
        await pk_cmd.finish("与菜逼PK发生未知错误，请联系管理员。")


@any_pk_cmd.handle()
async def handle_any_pk(event: MessageEvent, args: Message = CommandArg()):
    user_input = args.extract_plain_text().strip()
    qq = event.get_user_id()
    try:
        await _handle_any_pk(any_pk_cmd, qq, user_input)
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"PK操作报错: {e}")
        await any_pk_cmd.finish("PK发生未知错误，请联系管理员。")


@win_dist_cmd.handle()
async def handle_win_distribution(event: MessageEvent, args: Message = CommandArg()):
    user_input = args.extract_plain_text().strip()
    qq = event.get_user_id()
    try:
        await _handle_win_distribution(win_dist_cmd, qq, user_input, args)
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"胜场分布操作报错: {e}")
        await win_dist_cmd.finish("胜场分布生成发生未知错误，请联系管理员。")
