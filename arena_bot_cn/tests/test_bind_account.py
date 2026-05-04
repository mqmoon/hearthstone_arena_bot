import json
import time
import tempfile
import threading
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from arena_bot_cn.plugins import bind_account as b


# ============================================================
# Data layer tests
# ============================================================

class TestLoadSaveBindings:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.tmpdir = Path(tempfile.mkdtemp())
        monkeypatch.setattr(b, "DATA_DIR", self.tmpdir)
        b._bindings_cache = None
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        b._bindings_cache = None

    def test_load_empty_file(self):
        data = b._load_bindings()
        assert data == {"by_game_id": {}}

    def test_save_and_load_roundtrip(self):
        data = {"by_game_id": {"test": {"qq": "123", "name": "Test"}}}
        b._save_bindings(data)
        loaded = b._load_bindings()
        assert loaded == data

    def test_load_corrupt_json_returns_empty(self):
        bindings_path = b.DATA_DIR / "bindings.json"
        bindings_path.parent.mkdir(parents=True, exist_ok=True)
        bindings_path.write_text("{not valid", encoding="utf-8")
        data = b._load_bindings()
        assert data == {"by_game_id": {}}

    def test_memory_cache_hit(self):
        data = {"by_game_id": {"test": {"qq": "123", "name": "Test"}}}
        b._save_bindings(data)
        (b.DATA_DIR / "bindings.json").unlink()  # delete disk
        loaded = b._load_bindings()
        assert loaded == data  # from memory

    def test_memory_cache_updated_on_save(self):
        b._save_bindings({"by_game_id": {}})
        b._save_bindings({"by_game_id": {"x": {"qq": "1", "name": "X"}}})
        loaded = b._load_bindings()
        assert "x" in loaded["by_game_id"]

    def test_thread_safety_concurrent_loads(self):
        results = []
        errors = []

        def load():
            try:
                results.append(b._load_bindings())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=load) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert all(r == {"by_game_id": {}} for r in results)


class TestQueryBindings:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.tmpdir = Path(tempfile.mkdtemp())
        monkeypatch.setattr(b, "DATA_DIR", self.tmpdir)
        b._bindings_cache = None
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        b._bindings_cache = None

    def test_get_bindings_for_qq_empty(self):
        result = b._get_bindings_for_qq("123")
        assert result == []

    def test_get_bindings_for_qq_multiple(self):
        b._save_bindings({
            "by_game_id": {
                "a": {"qq": "111", "name": "A"},
                "b": {"qq": "111", "name": "B"},
                "c": {"qq": "222", "name": "C"},
            }
        })
        result = b._get_bindings_for_qq("111")
        assert len(result) == 2
        names = {r["name"] for r in result}
        assert names == {"A", "B"}

    def test_get_bindings_for_qq_case_preserved_name(self):
        b._save_bindings({
            "by_game_id": {
                "xm": {"qq": "111", "name": "XiaoMing"},
            }
        })
        result = b._get_bindings_for_qq("111")
        assert result[0]["name"] == "XiaoMing"

    def test_find_binding_by_game_id_lowercase(self):
        b._save_bindings({
            "by_game_id": {
                "xiaoming": {"qq": "111", "name": "小明"},
            }
        })
        assert b._find_binding_by_game_id("xiaoming") is not None
        assert b._find_binding_by_game_id("xiaoming")["qq"] == "111"

    def test_find_binding_by_game_id_not_found(self):
        assert b._find_binding_by_game_id("nobody") is None

    def test_game_id_uniqueness_across_qqs(self):
        """Same game_id cannot be bound to two different QQs."""
        b._save_bindings({
            "by_game_id": {
                "player1": {"qq": "111", "name": "Player1"},
            }
        })
        existing = b._find_binding_by_game_id("player1")
        assert existing is not None
        assert existing["qq"] == "111"


# ============================================================
# _parse_bind_args tests
# ============================================================

class TestParseBindArgs:
    def test_empty_input(self):
        assert b._parse_bind_args("") == ("list", None)

    def test_whitespace_only(self):
        assert b._parse_bind_args("   ") == ("list", None)

    def test_list_keyword(self):
        assert b._parse_bind_args("列表") == ("list", None)

    def test_list_keyword_with_trailing_space(self):
        assert b._parse_bind_args("列表 ") == ("list", None)

    def test_delete_with_target(self):
        assert b._parse_bind_args("删除 小明") == ("delete", "小明")

    def test_delete_with_spaces_in_target(self):
        assert b._parse_bind_args("删除 小明 的号") == ("delete", "小明 的号")

    def test_delete_without_target(self):
        assert b._parse_bind_args("删除") == ("list", None)

    def test_delete_empty_target_whitespace(self):
        assert b._parse_bind_args("删除 ") == ("list", None)

    def test_bind_simple(self):
        assert b._parse_bind_args("小明") == ("bind", "小明")

    def test_bind_with_leading_spaces(self):
        assert b._parse_bind_args("  小明") == ("bind", "小明")

    def test_bind_preserves_case(self):
        assert b._parse_bind_args("XiaoMing") == ("bind", "XiaoMing")


# ============================================================
# Command handler tests (mocked)
# ============================================================

class TestHandleList:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.tmpdir = Path(tempfile.mkdtemp())
        monkeypatch.setattr(b, "DATA_DIR", self.tmpdir)
        b._bindings_cache = None
        self.matcher = AsyncMock()
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        b._bindings_cache = None

    @pytest.mark.asyncio
    async def test_list_when_no_bindings(self):
        await b._handle_list(self.matcher, "111")
        reply = self.matcher.finish.call_args[0][0]
        assert "还没有绑定" in reply

    @pytest.mark.asyncio
    async def test_list_shows_all_bindings(self):
        b._save_bindings({
            "by_game_id": {
                "a": {"qq": "111", "name": "Alice"},
                "b": {"qq": "111", "name": "Bob"},
                "c": {"qq": "222", "name": "Charlie"},
            }
        })
        await b._handle_list(self.matcher, "111")
        reply = self.matcher.finish.call_args[0][0]
        assert "Alice" in reply
        assert "Bob" in reply
        assert "Charlie" not in reply
        assert "2" in reply or "二" in reply


class TestHandleBind:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.tmpdir = Path(tempfile.mkdtemp())
        monkeypatch.setattr(b, "DATA_DIR", self.tmpdir)
        b._bindings_cache = None
        self.matcher = AsyncMock()
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        b._bindings_cache = None

    @pytest.mark.asyncio
    async def test_bind_success(self):
        await b._handle_bind(self.matcher, "111", "小明")
        reply = self.matcher.finish.call_args[0][0]
        assert "绑定成功" in reply
        assert "小明" in reply

        data = b._load_bindings()
        assert data["by_game_id"]["小明"]["name"] == "小明"

    @pytest.mark.asyncio
    async def test_bind_empty_after_strip(self):
        await b._handle_bind(self.matcher, "111", "   ")
        reply = self.matcher.finish.call_args[0][0]
        assert "请输入要绑定的游戏ID" in reply

    @pytest.mark.asyncio
    async def test_bind_duplicate_same_qq(self):
        b._save_bindings({
            "by_game_id": {"小明": {"qq": "111", "name": "小明"}}
        })
        await b._handle_bind(self.matcher, "111", "小明")
        reply = self.matcher.finish.call_args[0][0]
        assert "已经绑定在你的账号下了" in reply

    @pytest.mark.asyncio
    async def test_bind_duplicate_different_qq(self):
        b._save_bindings({
            "by_game_id": {"小明": {"qq": "999", "name": "小明"}}
        })
        await b._handle_bind(self.matcher, "111", "小明")
        reply = self.matcher.finish.call_args[0][0]
        assert "已被其他用户绑定" in reply

    @pytest.mark.asyncio
    async def test_bind_case_insensitive_duplicate(self):
        b._save_bindings({
            "by_game_id": {"xiaoming": {"qq": "111", "name": "XiaoMing"}}
        })
        await b._handle_bind(self.matcher, "111", "xiaoming")
        reply = self.matcher.finish.call_args[0][0]
        assert "已经绑定在你的账号下了" in reply

    @pytest.mark.asyncio
    async def test_bind_case_preserved_in_storage(self):
        await b._handle_bind(self.matcher, "111", "XiaoMing")
        data = b._load_bindings()
        assert data["by_game_id"]["xiaoming"]["name"] == "XiaoMing"


class TestHandleDelete:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.tmpdir = Path(tempfile.mkdtemp())
        monkeypatch.setattr(b, "DATA_DIR", self.tmpdir)
        b._bindings_cache = None
        self.matcher = AsyncMock()
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        b._bindings_cache = None

    @pytest.mark.asyncio
    async def test_delete_success(self):
        b._save_bindings({
            "by_game_id": {"小明": {"qq": "111", "name": "小明"}}
        })
        await b._handle_delete(self.matcher, "111", "小明")
        reply = self.matcher.finish.call_args[0][0]
        assert "已解除" in reply

        data = b._load_bindings()
        assert "小明" not in data["by_game_id"]

    @pytest.mark.asyncio
    async def test_delete_case_insensitive(self):
        b._save_bindings({
            "by_game_id": {"xiaoming": {"qq": "111", "name": "小明"}}
        })
        await b._handle_delete(self.matcher, "111", "XIAOMING")
        reply = self.matcher.finish.call_args[0][0]
        assert "已解除" in reply

    @pytest.mark.asyncio
    async def test_delete_not_found(self):
        await b._handle_delete(self.matcher, "111", "nobody")
        reply = self.matcher.finish.call_args[0][0]
        assert "未在任何绑定记录中找到" in reply

    @pytest.mark.asyncio
    async def test_delete_wrong_qq(self):
        b._save_bindings({
            "by_game_id": {"小明": {"qq": "999", "name": "小明"}}
        })
        await b._handle_delete(self.matcher, "111", "小明")
        reply = self.matcher.finish.call_args[0][0]
        assert "不在你的绑定列表中" in reply

    @pytest.mark.asyncio
    async def test_delete_empty_target(self):
        await b._handle_delete(self.matcher, "111", "   ")
        reply = self.matcher.finish.call_args[0][0]
        assert "请输入要删除的游戏ID" in reply


# ============================================================
# Integration tests
# ============================================================

class TestIntegration:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.tmpdir = Path(tempfile.mkdtemp())
        monkeypatch.setattr(b, "DATA_DIR", self.tmpdir)
        b._bindings_cache = None
        self.matcher = AsyncMock()
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        b._bindings_cache = None

    @pytest.mark.asyncio
    async def test_full_bind_list_delete_cycle(self):
        # QQ1 binds two accounts
        await b._handle_bind(self.matcher, "111", "小明")
        await b._handle_bind(self.matcher, "111", "小红")

        # List
        await b._handle_list(self.matcher, "111")
        reply = self.matcher.finish.call_args[0][0]
        assert "小明" in reply
        assert "小红" in reply

        # Delete one
        await b._handle_delete(self.matcher, "111", "小明")

        # List again
        await b._handle_list(self.matcher, "111")
        reply = self.matcher.finish.call_args[0][0]
        assert "小明" not in reply
        assert "小红" in reply

        # Verify on disk
        data = b._load_bindings()
        assert "小明" not in data["by_game_id"]
        assert "小红" in data["by_game_id"]

    @pytest.mark.asyncio
    async def test_multi_qq_isolation(self):
        await b._handle_bind(self.matcher, "111", "Alice")
        await b._handle_bind(self.matcher, "222", "Bob")

        # QQ1 sees only Alice
        await b._handle_list(self.matcher, "111")
        reply = self.matcher.finish.call_args[0][0]
        assert "Alice" in reply
        assert "Bob" not in reply

        # QQ2 sees only Bob
        await b._handle_list(self.matcher, "222")
        reply = self.matcher.finish.call_args[0][0]
        assert "Bob" in reply
        assert "Alice" not in reply

    @pytest.mark.asyncio
    async def test_cannot_bind_same_id_to_different_qq(self):
        await b._handle_bind(self.matcher, "111", "SharedName")
        reply1 = self.matcher.finish.call_args[0][0]
        assert "绑定成功" in reply1

        await b._handle_bind(self.matcher, "222", "SharedName")
        reply2 = self.matcher.finish.call_args[0][0]
        assert "已被其他用户绑定" in reply2

    @pytest.mark.asyncio
    async def test_rebind_after_delete(self):
        """After QQ1 deletes a binding, QQ2 can bind it."""
        await b._handle_bind(self.matcher, "111", "FreeName")
        await b._handle_delete(self.matcher, "111", "FreeName")
        await b._handle_bind(self.matcher, "222", "FreeName")
        reply = self.matcher.finish.call_args[0][0]
        assert "绑定成功" in reply

        data = b._load_bindings()
        assert data["by_game_id"]["freename"]["qq"] == "222"

    @pytest.mark.asyncio
    async def test_disk_persistence(self):
        """Bindings survive memory cache clearing."""
        await b._handle_bind(self.matcher, "111", "Persist")

        # Clear memory cache
        b._bindings_cache = None

        data = b._load_bindings()
        assert "persist" in data["by_game_id"]
        assert data["by_game_id"]["persist"]["qq"] == "111"


# ============================================================
# _fuzzy_find_bindings tests
# ============================================================

class TestFuzzyFindBindings:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.tmpdir = Path(tempfile.mkdtemp())
        monkeypatch.setattr(b, "DATA_DIR", self.tmpdir)
        b._bindings_cache = None
        b._save_bindings({
            "by_game_id": {
                "xiaoming": {"qq": "111", "name": "XiaoMing"},
                "xiaohong": {"qq": "111", "name": "XiaoHong"},
                "charlie": {"qq": "222", "name": "Charlie"},
            }
        })
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        b._bindings_cache = None

    def test_empty_query(self):
        result = b._fuzzy_find_bindings("", "111")
        assert result == []

    def test_whitespace_only_query(self):
        result = b._fuzzy_find_bindings("   ", "111")
        assert result == []

    def test_substring_match_two(self):
        result = b._fuzzy_find_bindings("xiao", "111")
        assert len(result) == 2
        names = {r["name"] for r in result}
        assert names == {"XiaoMing", "XiaoHong"}

    def test_substring_match_one(self):
        result = b._fuzzy_find_bindings("ming", "111")
        assert len(result) == 1
        assert result[0]["name"] == "XiaoMing"

    def test_case_insensitive(self):
        result = b._fuzzy_find_bindings("XIAO", "111")
        assert len(result) == 2

    def test_only_matches_own_qq(self):
        """Should only match bindings belonging to the given QQ."""
        result = b._fuzzy_find_bindings("char", "111")
        assert result == []

    def test_no_match_for_other_qq(self):
        result = b._fuzzy_find_bindings("char", "222")
        assert len(result) == 1
        assert result[0]["name"] == "Charlie"

    def test_no_match_returns_empty(self):
        result = b._fuzzy_find_bindings("zzz", "111")
        assert result == []

    def test_preserves_original_case_in_name(self):
        result = b._fuzzy_find_bindings("ming", "111")
        assert result[0]["name"] == "XiaoMing"
