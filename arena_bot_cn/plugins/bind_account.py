import json
import threading
from pathlib import Path

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg
from nonebot.log import logger
from nonebot.exception import FinishedException

DATA_DIR = Path(__file__).parent.parent.parent / "data" / "arena_bot_cn"
DATA_DIR.mkdir(parents=True, exist_ok=True)

_bindings_cache: dict | None = None
_bindings_cache_lock = threading.Lock()

bind_cmd = on_command("账号绑定", aliases={"绑定账号", "bind"}, priority=10, block=True)


def _load_bindings() -> dict:
    global _bindings_cache
    with _bindings_cache_lock:
        if _bindings_cache is not None:
            return _bindings_cache

    path = DATA_DIR / "bindings.json"
    if not path.exists():
        data = {"by_game_id": {}}
    else:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {"by_game_id": {}}

    with _bindings_cache_lock:
        if _bindings_cache is None:
            _bindings_cache = data
        return _bindings_cache


def _save_bindings(data: dict) -> None:
    global _bindings_cache
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / "bindings.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with _bindings_cache_lock:
        _bindings_cache = data


def _get_bindings_for_qq(qq: str) -> list[dict]:
    data = _load_bindings()
    result = []
    for game_id_lower, entry in data.get("by_game_id", {}).items():
        if entry.get("qq") == qq:
            result.append({"game_id": game_id_lower, "name": entry.get("name", game_id_lower)})
    return result


def _find_binding_by_game_id(game_id_lower: str) -> dict | None:
    data = _load_bindings()
    return data.get("by_game_id", {}).get(game_id_lower)


def _parse_bind_args(user_input: str) -> tuple[str, str | None]:
    user_input = user_input.strip()

    if not user_input:
        return ("list", None)

    parts = user_input.split(maxsplit=1)

    if parts[0] == "列表":
        return ("list", None)

    if parts[0] == "删除":
        if len(parts) == 2 and parts[1].strip():
            return ("delete", parts[1].strip())
        return ("list", None)

    return ("bind", user_input)


async def _handle_list(matcher, qq: str):
    bindings = _get_bindings_for_qq(qq)
    if not bindings:
        await matcher.finish("你还没有绑定任何游戏账号。\n使用「账号绑定 游戏ID」进行绑定。")
        return

    lines = [f"你当前绑定了 {len(bindings)} 个游戏账号："]
    for i, b in enumerate(bindings, start=1):
        lines.append(f"{i}. {b['name']}")

    lines.append("\n使用「账号绑定 游戏ID」新增绑定")
    lines.append("使用「账号绑定 删除 游戏ID」解除绑定")
    await matcher.finish("\n".join(lines))


async def _handle_bind(matcher, qq: str, game_id: str):
    game_id = game_id.strip()
    if not game_id:
        await matcher.finish("请输入要绑定的游戏ID。\n用法：账号绑定 游戏ID")
        return

    game_id_lower = game_id.lower()

    existing = _find_binding_by_game_id(game_id_lower)
    if existing is not None:
        if existing["qq"] == qq:
            await matcher.finish(f"游戏账号【{game_id}】已经绑定在你的账号下了。")
        else:
            await matcher.finish(f"游戏账号【{game_id}】已被其他用户绑定，无法重复绑定。")
        return

    data = _load_bindings()
    if "by_game_id" not in data:
        data["by_game_id"] = {}

    data["by_game_id"][game_id_lower] = {"qq": qq, "name": game_id}
    _save_bindings(data)
    await matcher.finish(f"绑定成功！游戏账号【{game_id}】已关联到你的QQ。")


async def _handle_delete(matcher, qq: str, game_id: str):
    game_id = game_id.strip()
    if not game_id:
        await matcher.finish("请输入要删除的游戏ID。\n用法：账号绑定 删除 游戏ID")
        return

    game_id_lower = game_id.lower()

    existing = _find_binding_by_game_id(game_id_lower)
    if existing is None:
        await matcher.finish(f"游戏账号【{game_id}】未在任何绑定记录中找到。")
        return

    if existing["qq"] != qq:
        await matcher.finish(f"游戏账号【{game_id}】不在你的绑定列表中，无法删除。")
        return

    data = _load_bindings()
    del data["by_game_id"][game_id_lower]
    _save_bindings(data)
    await matcher.finish(f"已解除游戏账号【{game_id}】的绑定。")


@bind_cmd.handle()
async def handle_bind(event: MessageEvent, args: Message = CommandArg()):
    user_input = args.extract_plain_text().strip()
    qq = event.get_user_id()
    action, target = _parse_bind_args(user_input)

    try:
        if action == "list":
            await _handle_list(bind_cmd, qq)
        elif action == "bind":
            await _handle_bind(bind_cmd, qq, target)
        elif action == "delete":
            await _handle_delete(bind_cmd, qq, target)
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"账号绑定操作报错: {e}")
        await bind_cmd.finish("绑定操作发生未知错误，请联系管理员。")
