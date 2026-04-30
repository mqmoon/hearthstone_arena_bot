import json
import time
import tempfile
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import the module under test
from arena_bot_cn.plugins import query as q


# ============================================================
# parse_args — 参数解析
# ============================================================

class TestParseArgsNew:
    """新版地下竞技场 (is_old=False)"""

    def test_empty_input(self):
        sid, limit, name, player = q.parse_args("")
        assert sid == "6"
        assert limit == 10
        assert name == "最新赛季"
        assert player is None

    def test_single_digit_valid_season(self):
        sid, limit, name, player = q.parse_args("6")
        assert sid == "6"
        assert limit == 10
        assert name == "6"
        assert player is None

    def test_single_digit_min_season(self):
        sid, limit, name, player = q.parse_args("1")
        assert sid == "1"
        assert limit == 10

    def test_single_digit_out_of_range_is_limit(self):
        sid, limit, name, player = q.parse_args("100")
        assert sid == "6"
        assert limit == 100
        assert name == "最新赛季"
        assert player is None

    def test_single_digit_zero_clamped_to_1(self):
        sid, limit, name, player = q.parse_args("0")
        assert limit == 1

    def test_single_digit_7_out_of_range_is_limit(self):
        sid, limit, name, player = q.parse_args("7")
        assert sid == "6"
        assert limit == 7

    def test_single_alias_zuixin(self):
        sid, limit, name, player = q.parse_args("最新")
        assert sid == "6"
        assert limit == 10
        assert name == "最新"
        assert player is None

    def test_single_alias_shangsaiji(self):
        sid, limit, name, player = q.parse_args("上赛季")
        assert sid == "5"
        assert limit == 10
        assert name == "上赛季"
        assert player is None

    def test_single_alias_default(self):
        sid, limit, name, player = q.parse_args("默认")
        assert sid == "6"
        assert limit == 10

    def test_single_non_alias_is_player_name(self):
        sid, limit, name, player = q.parse_args("小明")
        assert sid == "6"
        assert limit == 10
        assert name == "最新赛季"
        assert player == "小明"

    def test_season_num_with_limit(self):
        sid, limit, name, player = q.parse_args("5 50")
        assert sid == "5"
        assert limit == 50
        assert name == "5"
        assert player is None

    def test_season_alias_with_limit(self):
        sid, limit, name, player = q.parse_args("最新 100")
        assert sid == "6"
        assert limit == 100
        assert name == "最新"
        assert player is None

    def test_season_num_with_player_name(self):
        sid, limit, name, player = q.parse_args("6 小明")
        assert sid == "6"
        assert limit == 10
        assert name == "6"
        assert player == "小明"

    def test_season_alias_with_player_name(self):
        sid, limit, name, player = q.parse_args("上赛季 小红")
        assert sid == "5"
        assert limit == 10
        assert name == "上赛季"
        assert player == "小红"

    def test_invalid_season_text_returns_none(self):
        sid, limit, name, player = q.parse_args("invalid 50")
        assert sid is None
        assert name == "INVALID"

    def test_digit_0_as_season_not_valid(self):
        """0 is not a valid season ID for new arena"""
        sid, limit, name, player = q.parse_args("0")
        assert sid == "6"
        assert limit == 1  # clamped from 0


class TestParseArgsOld:
    """旧版竞技场 (is_old=True)"""

    def test_empty_input(self):
        sid, limit, name, player = q.parse_args("", is_old=True)
        assert sid == "56"
        assert limit == 10
        assert name == "56"
        assert player is None

    def test_single_digit_valid_season(self):
        sid, limit, name, player = q.parse_args("50", is_old=True)
        assert sid == "50"
        assert limit == 10

    def test_single_digit_min_old_season(self):
        sid, limit, name, player = q.parse_args("49", is_old=True)
        assert sid == "49"

    def test_single_digit_out_of_range_is_limit(self):
        sid, limit, name, player = q.parse_args("100", is_old=True)
        assert sid == "56"
        assert limit == 100
        assert player is None

    def test_single_digit_48_out_of_range_is_limit(self):
        """48 is below old season range, treated as limit"""
        sid, limit, name, player = q.parse_args("48", is_old=True)
        assert sid == "56"
        assert limit == 48

    def test_single_alias_zuixin(self):
        sid, limit, name, player = q.parse_args("最新", is_old=True)
        assert sid == "56"
        assert name == "最新"

    def test_single_alias_shangsaiji(self):
        sid, limit, name, player = q.parse_args("上赛季", is_old=True)
        assert sid == "55"
        assert name == "上赛季"

    def test_single_alias_default(self):
        sid, limit, name, player = q.parse_args("默认", is_old=True)
        assert sid == "56"
        assert name == "默认"

    def test_single_non_alias_is_player_name(self):
        sid, limit, name, player = q.parse_args("小明", is_old=True)
        assert sid == "56"
        assert limit == 10
        assert name == "56"
        assert player == "小明"

    def test_season_num_with_player_name(self):
        sid, limit, name, player = q.parse_args("50 小明", is_old=True)
        assert sid == "50"
        assert limit == 10
        assert player == "小明"

    def test_season_alias_with_player_name(self):
        sid, limit, name, player = q.parse_args("上赛季 小红", is_old=True)
        assert sid == "55"
        assert player == "小红"

    def test_max_old_season(self):
        sid, limit, name, player = q.parse_args("56", is_old=True)
        assert sid == "56"


class TestParseArgsEdgeCases:
    """边界与组合"""

    def test_player_name_with_spaces(self):
        """名带空格的会被 split 拆开，last non-digit 为名"""
        sid, limit, name, player = q.parse_args("6 小明 小红")
        assert sid is None  # "6 小明" is not a valid season key
        assert player == "小红"

    def test_large_limit(self):
        sid, limit, name, player = q.parse_args("最新 999")
        assert limit == 999

    def test_season_by_number_string(self):
        """数字字符串直接作为赛季号"""
        sid, limit, name, player = q.parse_args("3 30")
        assert sid == "3"
        assert limit == 30


# ============================================================
# _cache_path
# ============================================================

class TestCachePath:
    def test_new_arena_path(self):
        path = q._cache_path("undergroundarena", "6", 1)
        parts = path.parts
        assert "undergroundarena" in parts
        assert "season_6" in parts
        assert path.name == "page_1.json"

    def test_old_arena_path(self):
        path = q._cache_path("arena", "56", 3)
        assert "arena" in str(path)
        assert "season_56" in str(path)
        assert path.name == "page_3.json"


# ============================================================
# _path_to_cache_key
# ============================================================

class TestPathToCacheKey:
    def test_valid_path(self):
        path = q._cache_path("undergroundarena", "6", 2)
        key = q._path_to_cache_key(path)
        assert key == ("undergroundarena", "6", 2)

    def test_old_arena_path(self):
        path = q._cache_path("arena", "55", 5)
        key = q._path_to_cache_key(path)
        assert key == ("arena", "55", 5)

    def test_invalid_path_returns_none(self):
        path = Path("/tmp/not/a/cache/file.json")
        key = q._path_to_cache_key(path)
        assert key is None

    def test_nested_invalid_path_no_underscore_in_season(self):
        path = Path("/tmp/foo/bar/baz/page_1.json")
        key = q._path_to_cache_key(path)
        assert key is None  # "baz" has no "_" → ValueError → None


# ============================================================
# _find_player_in_page
# ============================================================

class TestFindPlayerInPage:
    def test_finds_player_case_insensitive(self):
        players = [
            {"battle_tag": "小明#1234", "position": 1, "score": 9999},
            {"battle_tag": "小红#5678", "position": 2, "score": 8888},
        ]
        result = q._find_player_in_page(players, "小明")
        assert result is not None
        assert result["battle_tag"] == "小明#1234"

    def test_finds_player_different_case(self):
        players = [{"battle_tag": "PlayerName#1234", "position": 1}]
        result = q._find_player_in_page(players, "playername")
        assert result is not None

    def test_returns_none_when_not_found(self):
        players = [{"battle_tag": "AAA#1111", "position": 1}]
        result = q._find_player_in_page(players, "BBB")
        assert result is None

    def test_handles_missing_battle_tag(self):
        players = [{"position": 1, "score": 100}]
        result = q._find_player_in_page(players, "anything")
        assert result is None

    def test_empty_list(self):
        result = q._find_player_in_page([], "nobody")
        assert result is None

    def test_tag_without_hash(self):
        players = [{"battle_tag": "NoHash", "position": 1}]
        result = q._find_player_in_page(players, "NoHash")
        assert result is not None

    def test_exact_name_match(self):
        players = [
            {"battle_tag": "abc#1234", "position": 1},
            {"battle_tag": "abcdef#5678", "position": 2},
        ]
        result = q._find_player_in_page(players, "abc")
        assert result is not None
        assert result["battle_tag"] == "abc#1234"


# ============================================================
# _resolve_cache_ttl
# ============================================================

class TestResolveCacheTTL:
    def test_latest_underground_has_ttl(self):
        assert q._resolve_cache_ttl("undergroundarena", "6") == q.CACHE_TTL

    def test_old_underground_no_ttl(self):
        assert q._resolve_cache_ttl("undergroundarena", "5") is None
        assert q._resolve_cache_ttl("undergroundarena", "1") is None

    def test_old_arena_no_ttl(self):
        assert q._resolve_cache_ttl("arena", "56") is None
        assert q._resolve_cache_ttl("arena", "50") is None

    def test_unknown_mode_no_ttl(self):
        assert q._resolve_cache_ttl("unknown", "1") is None


# ============================================================
# _rank_prefix
# ============================================================

class TestRankPrefix:
    def test_top_10(self):
        assert q._rank_prefix(1) == "[前10] "
        assert q._rank_prefix(10) == "[前10] "

    def test_top_25(self):
        assert q._rank_prefix(11) == "[前25] "
        assert q._rank_prefix(25) == "[前25] "

    def test_top_50(self):
        assert q._rank_prefix(26) == "[前50] "
        assert q._rank_prefix(50) == "[前50] "

    def test_top_100(self):
        assert q._rank_prefix(51) == "[前100] "
        assert q._rank_prefix(100) == "[前100] "

    def test_beyond_100(self):
        assert q._rank_prefix(101) == " " * 11

    def test_all_prefixes_end_with_space(self):
        """All rank prefixes should end with a space for formatting consistency."""
        for pos in [1, 11, 26, 51]:
            assert q._rank_prefix(pos).endswith(" "), f"Position {pos} prefix missing trailing space"


# ============================================================
# _should_cache
# ============================================================

class TestShouldCache:
    def test_always_true(self):
        assert q._should_cache("undergroundarena", "6") is True
        assert q._should_cache("undergroundarena", "1") is True
        assert q._should_cache("arena", "56") is True
        assert q._should_cache("unknown", "0") is True


# ============================================================
# _load_cache / _save_cache — 磁盘 + 内存缓存
# ============================================================

class TestLoadSaveCache:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        """Redirect DATA_DIR to a temp dir for isolated testing."""
        self.tmpdir = Path(tempfile.mkdtemp())
        monkeypatch.setattr(q, "DATA_DIR", self.tmpdir)
        # Clear memory cache between tests
        q._mem_cache.clear()
        yield
        # Cleanup
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        q._mem_cache.clear()

    def test_save_and_load(self):
        path = q._cache_path("undergroundarena", "5", 1)
        data = [{"battle_tag": "Test#1234", "position": 1}]
        q._save_cache(path, data)

        loaded = q._load_cache(path)
        assert loaded == data

    def test_load_nonexistent(self):
        path = q._cache_path("undergroundarena", "1", 99)
        result = q._load_cache(path)
        assert result is None

    def test_ttl_expired_returns_none(self, monkeypatch):
        path = q._cache_path("undergroundarena", "6", 1)
        data = [{"battle_tag": "Test#1234"}]
        q._save_cache(path, data)

        # Advance time past TTL
        future = time.time() + q.CACHE_TTL + 10
        monkeypatch.setattr(time, "time", lambda: future)

        loaded = q._load_cache(path, ttl=q.CACHE_TTL)
        assert loaded is None

    def test_ttl_none_always_loads(self):
        path = q._cache_path("arena", "55", 1)
        data = [{"battle_tag": "Old#1234"}]
        q._save_cache(path, data)

        # Set file mtime far in the past
        past = time.time() - 100000
        path.touch()
        import os
        os.utime(str(path), (past, past))

        loaded = q._load_cache(path, ttl=None)
        assert loaded == data

    def test_memory_cache_hit(self):
        path = q._cache_path("undergroundarena", "5", 1)
        data = [{"battle_tag": "Mem#1234"}]
        q._save_cache(path, data)

        # Delete disk cache to prove memory hit
        path.unlink()

        loaded = q._load_cache(path)
        assert loaded == data

    def test_memory_cache_expiry_for_ttl_season(self, monkeypatch):
        path = q._cache_path("undergroundarena", "6", 1)
        data = [{"battle_tag": "Expire#1234"}]
        q._save_cache(path, data)

        # Advance time past TTL
        future = time.time() + q.CACHE_TTL + 10
        monkeypatch.setattr(time, "time", lambda: future)

        # Memory cache should be expired, disk check will be done
        # File exists but TTL expired -> returns None
        loaded = q._load_cache(path, ttl=q.CACHE_TTL)
        assert loaded is None

    def test_memory_cache_permanent_for_no_ttl(self, monkeypatch):
        path = q._cache_path("arena", "50", 1)
        data = [{"battle_tag": "Perm#1234"}]
        q._save_cache(path, data)

        # Delete disk to force memory-only
        path.unlink()

        # Advance time past CACHE_TTL
        future = time.time() + q.CACHE_TTL * 10
        monkeypatch.setattr(time, "time", lambda: future)

        # ttl=None means permanent, memory cache should still hit
        loaded = q._load_cache(path, ttl=None)
        assert loaded == data

    def test_corrupt_json_returns_none(self):
        path = q._cache_path("undergroundarena", "1", 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")

        result = q._load_cache(path)
        assert result is None


# ============================================================
# _fetch_all_pages — async
# ============================================================

class TestFetchAllPages:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.tmpdir = Path(tempfile.mkdtemp())
        monkeypatch.setattr(q, "DATA_DIR", self.tmpdir)
        q._mem_cache.clear()
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        q._mem_cache.clear()

    @pytest.mark.asyncio
    async def test_all_cached_no_api_calls(self, monkeypatch):
        """When all pages are cached, API should not be called."""
        for page in range(1, 3):
            path = q._cache_path("arena", "50", page)
            q._save_cache(path, [{"battle_tag": f"Player{page}#1234", "position": page}])

        # Patch _fetch_page_from_api to ensure it's not called
        mock_fetch = AsyncMock()
        monkeypatch.setattr(q, "_fetch_page_from_api", mock_fetch)

        result = await q._fetch_all_pages("arena", "50", 2, use_cache=True)
        assert len(result) == 2
        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_mixed_cache_and_api(self, monkeypatch):
        """Page 1 cached, page 2 fetched from API."""
        q._save_cache(
            q._cache_path("arena", "50", 1),
            [{"battle_tag": "Cached#1234", "position": 1}],
        )

        async def mock_fetch(client, mode, season_id, page):
            return [{"battle_tag": f"Fetched{page}#1234", "position": page}]

        monkeypatch.setattr(q, "_fetch_page_from_api", mock_fetch)

        result = await q._fetch_all_pages("arena", "50", 2, use_cache=True)
        assert len(result) == 2
        tags = [p["battle_tag"] for p in result]
        assert "Cached#1234" in tags
        assert "Fetched2#1234" in tags

    @pytest.mark.asyncio
    async def test_no_cache_uses_api_for_all(self, monkeypatch):
        call_count = 0

        async def mock_fetch(client, mode, season_id, page):
            nonlocal call_count
            call_count += 1
            return [{"battle_tag": f"API{page}#1234", "position": page}]

        monkeypatch.setattr(q, "_fetch_page_from_api", mock_fetch)

        result = await q._fetch_all_pages("arena", "50", 2, use_cache=False)
        assert call_count == 2
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_ttl_expired_refetches(self, monkeypatch):
        """Cached page with expired TTL should be refetched."""
        path = q._cache_path("undergroundarena", "6", 1)
        q._save_cache(path, [{"battle_tag": "Stale#1234", "position": 1}])

        api_called = False

        async def mock_fetch(client, mode, season_id, page):
            nonlocal api_called
            api_called = True
            return [{"battle_tag": "Fresh#1234", "position": 1}]

        monkeypatch.setattr(q, "_fetch_page_from_api", mock_fetch)
        # Override _load_cache to simulate TTL expiry on this specific call
        original_load = q._load_cache

        def mock_load(p, ttl=None):
            if p == path:
                return None  # Simulate TTL expired
            return original_load(p, ttl)

        monkeypatch.setattr(q, "_load_cache", mock_load)

        result = await q._fetch_all_pages("undergroundarena", "6", 1, use_cache=True, ttl=3600)
        assert api_called
        assert result[0]["battle_tag"] == "Fresh#1234"


# ============================================================
# _search_player_in_season — async
# ============================================================

class TestSearchPlayerInSeason:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.tmpdir = Path(tempfile.mkdtemp())
        monkeypatch.setattr(q, "DATA_DIR", self.tmpdir)
        q._mem_cache.clear()
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        q._mem_cache.clear()

    @pytest.mark.asyncio
    async def test_finds_player_in_first_page_cache(self):
        q._save_cache(
            q._cache_path("arena", "50", 1),
            [{"battle_tag": "Target#1234", "position": 5, "score": 9000}],
        )

        result = await q._search_player_in_season("arena", "50", "Target")
        assert result is not None
        assert result["position"] == 5
        assert result["score"] == 9000

    @pytest.mark.asyncio
    async def test_finds_player_in_later_page_via_api(self, monkeypatch):
        page2 = None

        async def mock_fetch(client, mode, season_id, page):
            nonlocal page2
            if page == 1:
                return [{"battle_tag": "Other#1234", "position": 1}]
            elif page == 2:
                page2 = [{"battle_tag": "Target#5678", "position": 30, "score": 7000}]
                return page2
            return []

        monkeypatch.setattr(q, "_fetch_page_from_api", mock_fetch)

        result = await q._search_player_in_season("undergroundarena", "6", "Target")
        assert result is not None
        assert result["position"] == 30

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self, monkeypatch):
        async def mock_fetch(client, mode, season_id, page):
            return [] if page > 1 else [{"battle_tag": "Other#1234", "position": 1}]

        monkeypatch.setattr(q, "_fetch_page_from_api", mock_fetch)

        result = await q._search_player_in_season("arena", "50", "Nobody")
        assert result is None

    @pytest.mark.asyncio
    async def test_stops_early_on_match(self, monkeypatch):
        """Should stop fetching after finding the player."""
        pages_fetched = []

        async def mock_fetch(client, mode, season_id, page):
            pages_fetched.append(page)
            if page == 1:
                return [{"battle_tag": "Hit#1234", "position": 1}]
            return [{"battle_tag": f"Extra{page}#1234", "position": page}]

        monkeypatch.setattr(q, "_fetch_page_from_api", mock_fetch)

        result = await q._search_player_in_season("arena", "50", "Hit")
        assert result is not None
        assert pages_fetched == [1]  # Only page 1 was fetched


# ============================================================
# _get_cached_season_all — async
# ============================================================

class TestGetCachedSeasonAll:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.tmpdir = Path(tempfile.mkdtemp())
        monkeypatch.setattr(q, "DATA_DIR", self.tmpdir)
        q._mem_cache.clear()
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        q._mem_cache.clear()

    @pytest.mark.asyncio
    async def test_empty_dir(self):
        result = await q._get_cached_season_all("undergroundarena", "1")
        assert result == []

    @pytest.mark.asyncio
    async def test_loads_all_pages(self):
        for page in range(1, 4):
            q._save_cache(
                q._cache_path("arena", "55", page),
                [{"battle_tag": f"P{page}#1234", "position": page}],
            )

        result = await q._get_cached_season_all("arena", "55")
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_pages_sorted(self):
        q._save_cache(q._cache_path("arena", "55", 2), [{"battle_tag": "P2#1234", "position": 2}])
        q._save_cache(q._cache_path("arena", "55", 1), [{"battle_tag": "P1#1234", "position": 1}])

        result = await q._get_cached_season_all("arena", "55")
        assert result[0]["position"] == 1
        assert result[1]["position"] == 2


# ============================================================
# SEASON_ALIASES / OLD_SEASON_ALIASES
# ============================================================

class TestSeasonAliases:
    def test_new_season_aliases(self):
        assert q.SEASON_ALIASES["最新"] == "6"
        assert q.SEASON_ALIASES["上赛季"] == "5"
        assert q.SEASON_ALIASES["默认"] == "6"

    def test_old_season_aliases(self):
        assert q.OLD_SEASON_ALIASES["最新"] == "56"
        assert q.OLD_SEASON_ALIASES["上赛季"] == "55"
        assert q.OLD_SEASON_ALIASES["默认"] == "56"

    def test_all_new_seasons(self):
        assert q.ALL_NEW_SEASONS == ["1", "2", "3", "4", "5", "6"]

    def test_all_old_seasons(self):
        assert q.ALL_OLD_SEASONS == [str(i) for i in range(49, 57)]
        assert len(q.ALL_OLD_SEASONS) == 8
