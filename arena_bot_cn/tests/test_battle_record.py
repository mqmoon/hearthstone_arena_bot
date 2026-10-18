import json
import time
import tempfile
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from arena_bot_cn.plugins import battle_record as br
from arena_bot_cn.plugins import bind_account as b


# ============================================================
# Helpers
# ============================================================

def _setup_bindings(tmpdir: Path, data: dict):
    """Write bindings data and clear caches so battle_record can read them."""
    b._bindings_cache = None
    bindings_path = tmpdir / "bindings.json"
    bindings_path.parent.mkdir(parents=True, exist_ok=True)
    with open(bindings_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============================================================
# Data layer tests
# ============================================================

class TestLoadSaveRecords:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.tmpdir = Path(tempfile.mkdtemp())
        monkeypatch.setattr(br, "DATA_DIR", self.tmpdir)
        br._records_cache = None
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        br._records_cache = None

    def test_load_empty_file(self):
        data = br._load_records()
        assert data == {"by_game_id": {}}

    def test_save_and_load_roundtrip(self):
        data = {
            "by_game_id": {
                "xiaoming": {
                    "rounds": [{"wins": 5, "score": 500.0}]
                }
            }
        }
        br._save_records(data)
        loaded = br._load_records()
        assert loaded == data

    def test_load_corrupt_json_returns_empty(self):
        path = br.DATA_DIR / "battle_records.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid", encoding="utf-8")
        data = br._load_records()
        assert data == {"by_game_id": {}}

    def test_memory_cache_hit(self):
        data = {"by_game_id": {"test": {"rounds": [{"wins": 3, "score": 300.0}]}}}
        br._save_records(data)
        (br.DATA_DIR / "battle_records.json").unlink()
        loaded = br._load_records()
        assert loaded == data

    def test_memory_cache_updated_on_save(self):
        br._save_records({"by_game_id": {}})
        br._save_records({"by_game_id": {"x": {"rounds": [{"wins": 1, "score": 100.0}]}}})
        loaded = br._load_records()
        assert "x" in loaded["by_game_id"]

    def test_thread_safety_concurrent_loads(self):
        results = []
        errors = []

        def load():
            try:
                results.append(br._load_records())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=load) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert all(r == {"by_game_id": {}} for r in results)


class TestRecalcScores:
    def test_empty_list(self):
        rounds = []
        br._recalc_scores(rounds)
        assert rounds == []

    def test_single_round(self):
        rounds = [{"wins": 5, "score": 0}]
        br._recalc_scores(rounds)
        assert rounds[0]["score"] == 500.0
        assert rounds[0]["avg_wins"] == 5.0

    def test_two_rounds_ema(self):
        rounds = [{"wins": 5, "score": 0}, {"wins": 3, "score": 0}]
        br._recalc_scores(rounds)
        assert rounds[0]["score"] == 500.0
        assert rounds[1]["score"] == 500.0 * 0.9 + 3 * 100  # 750.0
        assert rounds[0]["avg_wins"] == 5.0
        assert rounds[1]["avg_wins"] == 4.0  # (5+3)/2

    def test_three_rounds_ema(self):
        rounds = [{"wins": 5, "score": 0}, {"wins": 3, "score": 0}, {"wins": 7, "score": 0}]
        br._recalc_scores(rounds)
        assert rounds[0]["score"] == 500.0
        assert rounds[1]["score"] == 750.0
        assert rounds[2]["score"] == round(750.0 * 0.9 + 7 * 100, 1)  # 1375.0
        assert rounds[2]["avg_wins"] == 5.0  # (5+3+7)/3

    def test_avg_wins_ema_after_30(self):
        """After 30 rounds, avg_wins switches from simple mean to EMA(1/30)."""
        rounds = [{"wins": 10, "score": 0} for _ in range(31)]
        br._recalc_scores(rounds)
        # Round 30 (index 29): simple mean = 10.0
        assert rounds[29]["avg_wins"] == 10.0
        # Round 31 (index 30): EMA = 10.0 * 29/30 + 10 * 1/30 = 10.0
        assert rounds[30]["avg_wins"] == 10.0

    def test_avg_wins_ema_decay(self):
        """EMA avg_wins should decay when wins drop."""
        rounds = [{"wins": 10, "score": 0} for _ in range(30)]
        rounds.append({"wins": 0, "score": 0})
        br._recalc_scores(rounds)
        # After 30 rounds of 10: avg = 10.0; round 31: 10.0 * 29/30 + 0 * 1/30 ≈ 9.67
        assert rounds[30]["avg_wins"] == round(10.0 * 29 / 30, 2)

    def test_ema_decays_over_time(self):
        """Scores decay: 12 wins then 0 wins repeatedly should show decay."""
        rounds = [{"wins": 12, "score": 0}]
        for _ in range(10):
            rounds.append({"wins": 0, "score": 0})
        br._recalc_scores(rounds)
        # After 10 zero-win rounds, score should decay significantly from 1200
        assert rounds[0]["score"] == 1200.0
        assert rounds[-1]["score"] < rounds[0]["score"]  # decay happened

    def test_ema_converges_to_1000_when_always_10_wins(self):
        """10 wins * 100 = 1000. EMA should converge to 1000 / (1-0.9) = 10000? No wait...
        EMA formula: s_n = s_{n-1} * 0.9 + 1000. Steady state: s = s*0.9 + 1000 => s*0.1 = 1000 => s = 10000."""
        rounds = []
        for _ in range(50):
            rounds.append({"wins": 10, "score": 0})
        br._recalc_scores(rounds)
        # Should be close to 10000
        assert 9900 < rounds[-1]["score"] < 10010


# ============================================================
# Arg parsing tests
# ============================================================

class TestParseUploadArgs:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.tmpdir = Path(tempfile.mkdtemp())
        monkeypatch.setattr(b, "DATA_DIR", self.tmpdir)
        monkeypatch.setattr(br, "DATA_DIR", self.tmpdir)
        b._bindings_cache = None
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        b._bindings_cache = None

    def _bind(self, game_id, qq="111", name=None):
        data = b._load_bindings()
        if "by_game_id" not in data:
            data["by_game_id"] = {}
        data["by_game_id"][game_id.lower()] = {"qq": qq, "name": name or game_id}
        b._save_bindings(data)

    # ── Error: empty / short input ──

    def test_empty_input(self):
        _, _, _, _, _, error = br._parse_upload_args("", "111")
        assert error is not None
        assert "战绩上传" in error

    def test_only_game_id_not_enough(self):
        self._bind("xiaoming", qq="111")
        _, _, _, _, _, error = br._parse_upload_args("xiaoming", "111")
        assert error is not None
        assert "职业" in error

    def test_too_few_args(self):
        self._bind("xiaoming", qq="111")
        _, _, _, _, _, error = br._parse_upload_args("xiaoming 法师", "111")
        assert error is not None
        assert "职业" in error and "胜场" in error

    # ── Game ID resolution ──

    def test_game_id_not_bound(self):
        _, _, _, _, _, error = br._parse_upload_args("nobody 法师 5 3", "111")
        assert "未绑定" in error

    def test_game_id_bound_to_other_qq(self):
        self._bind("xiaoming", qq="999", name="小明")
        _, _, _, _, _, error = br._parse_upload_args("xiaoming 法师 5 3", "111")
        assert "未绑定" in error

    def test_no_game_id_uses_first_binding(self):
        self._bind("xiaoming", qq="111", name="小明")
        self._bind("xiaohong", qq="111", name="小红")
        game_id_lower, display_name, class_name, wins, losses, error = br._parse_upload_args("法师 5 3", "111")
        assert error is None
        assert game_id_lower == "xiaoming"
        assert display_name == "小明"
        assert class_name == "法师"
        assert wins == 5
        assert losses == 3

    def test_no_game_id_no_bindings(self):
        _, _, _, _, _, error = br._parse_upload_args("法师 5 3", "111")
        assert "还没有绑定" in error

    def test_case_insensitive_game_id(self):
        self._bind("XiaoMing", qq="111", name="XiaoMing")
        game_id_lower, _, _, _, _, error = br._parse_upload_args("xiaoming 法师 5 3", "111")
        assert error is None
        assert game_id_lower == "xiaoming"

    # ── Class resolution ──

    def test_canonical_class_name(self):
        self._bind("xiaoming", qq="111")
        _, _, class_name, _, _, error = br._parse_upload_args("xiaoming 法师 5 3", "111")
        assert error is None
        assert class_name == "法师"

    def test_class_alias_dk(self):
        self._bind("xiaoming", qq="111")
        _, _, class_name, _, _, error = br._parse_upload_args("xiaoming dk 5 3", "111")
        assert error is None
        assert class_name == "死亡骑士"

    def test_class_alias_uppercase(self):
        self._bind("xiaoming", qq="111")
        _, _, class_name, _, _, error = br._parse_upload_args("xiaoming DK 5 3", "111")
        assert error is None
        assert class_name == "死亡骑士"

    def test_class_alias_dh(self):
        self._bind("xiaoming", qq="111")
        _, _, class_name, _, _, error = br._parse_upload_args("xiaoming dh 5 3", "111")
        assert error is None
        assert class_name == "恶魔猎手"

    def test_invalid_class(self):
        self._bind("xiaoming", qq="111")
        _, _, _, _, _, error = br._parse_upload_args("xiaoming 武僧 5 3", "111")
        assert "不是合法的职业名称" in error

    # ── Win/loss validation ──

    def test_wins_0_losses_3_valid(self):
        self._bind("xiaoming", qq="111")
        _, _, _, wins, losses, error = br._parse_upload_args("xiaoming 法师 0 3", "111")
        assert error is None
        assert wins == 0
        assert losses == 3

    def test_wins_above_12(self):
        self._bind("xiaoming", qq="111")
        _, _, _, _, _, error = br._parse_upload_args("xiaoming 法师 13 3", "111")
        assert "超出合法范围" in error
        assert "13" in error

    def test_non_numeric_wins(self):
        self._bind("xiaoming", qq="111")
        _, _, _, _, _, error = br._parse_upload_args("xiaoming 法师 abc 3", "111")
        assert "不是有效数字" in error

    def test_non_numeric_losses(self):
        self._bind("xiaoming", qq="111")
        _, _, _, _, _, error = br._parse_upload_args("xiaoming 法师 5 xyz", "111")
        assert "不是有效数字" in error

    def test_wins_1_to_11_losses_must_be_3(self):
        self._bind("xiaoming", qq="111")
        for w in [1, 5, 11]:
            _, _, _, _, _, error = br._parse_upload_args(f"xiaoming 法师 {w} 2", "111")
            assert error is not None
            assert "负场必须为3" in error

    def test_wins_12_losses_0_1_2_valid(self):
        self._bind("xiaoming", qq="111")
        for lo in [0, 1, 2]:
            _, _, _, wins, losses, error = br._parse_upload_args(f"xiaoming 法师 12 {lo}", "111")
            assert error is None, f"losses={lo} should be valid"
            assert wins == 12
            assert losses == lo

    def test_wins_12_losses_3_invalid(self):
        self._bind("xiaoming", qq="111")
        _, _, _, _, _, error = br._parse_upload_args("xiaoming 法师 12 3", "111")
        assert "负场只能为0/1/2" in error

    def test_wins_5_losses_3_valid(self):
        self._bind("xiaoming", qq="111")
        _, _, _, wins, losses, error = br._parse_upload_args("xiaoming 法师 5 3", "111")
        assert error is None
        assert wins == 5
        assert losses == 3

    def test_wins_1_losses_3_valid(self):
        self._bind("xiaoming", qq="111")
        _, _, _, wins, losses, error = br._parse_upload_args("xiaoming 法师 1 3", "111")
        assert error is None
        assert wins == 1
        assert losses == 3

    # ── Game ID with spaces ──

    def test_game_id_with_spaces(self):
        self._bind("player with space", qq="111", name="有空格")
        game_id_lower, display_name, class_name, wins, losses, error = br._parse_upload_args(
            "player with space 法师 5 3", "111"
        )
        assert error is None
        assert game_id_lower == "player with space"
        assert display_name == "有空格"


class TestResolveGameId:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.tmpdir = Path(tempfile.mkdtemp())
        monkeypatch.setattr(b, "DATA_DIR", self.tmpdir)
        b._bindings_cache = None
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        b._bindings_cache = None

    def _bind(self, game_id, qq="111", name=None):
        data = b._load_bindings()
        if "by_game_id" not in data:
            data["by_game_id"] = {}
        data["by_game_id"][game_id.lower()] = {"qq": qq, "name": name or game_id}
        b._save_bindings(data)

    def test_empty_input_no_bindings(self):
        _, _, error = br._resolve_game_id("", "111")
        assert "还没有绑定" in error

    def test_empty_input_with_bindings(self):
        self._bind("xiaoming", qq="111", name="小明")
        self._bind("xiaohong", qq="111", name="小红")
        game_id_lower, display_name, error = br._resolve_game_id("", "111")
        assert error is None
        assert game_id_lower == "xiaoming"
        assert display_name == "小明"

    def test_specific_game_id_found(self):
        self._bind("xiaoming", qq="111", name="小明")
        game_id_lower, display_name, error = br._resolve_game_id("xiaoming", "111")
        assert error is None
        assert game_id_lower == "xiaoming"
        assert display_name == "小明"

    def test_game_id_not_found(self):
        _, _, error = br._resolve_game_id("nobody", "111")
        assert "未找到绑定记录" in error

    def test_game_id_bound_to_other_qq(self):
        self._bind("xiaoming", qq="999", name="小明")
        _, _, error = br._resolve_game_id("xiaoming", "111")
        assert "不在你的绑定列表中" in error


class TestParseDeleteArgs:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.tmpdir = Path(tempfile.mkdtemp())
        monkeypatch.setattr(b, "DATA_DIR", self.tmpdir)
        b._bindings_cache = None
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        b._bindings_cache = None

    def _bind(self, game_id, qq="111", name=None):
        data = b._load_bindings()
        if "by_game_id" not in data:
            data["by_game_id"] = {}
        data["by_game_id"][game_id.lower()] = {"qq": qq, "name": name or game_id}
        b._save_bindings(data)

    def test_empty_input_has_bindings_no_indices(self):
        self._bind("xiaoming", qq="111", name="小明")
        game_id_lower, display_name, indices, error = br._parse_delete_args("", "111")
        assert error is not None
        assert "轮次索引" in error
        # game_id should still be resolved
        assert game_id_lower == "xiaoming"

    def test_empty_input_no_bindings(self):
        _, _, _, error = br._parse_delete_args("", "111")
        assert "还没有绑定" in error

    def test_game_id_with_indices(self):
        self._bind("xiaoming", qq="111", name="小明")
        game_id_lower, display_name, indices, error = br._parse_delete_args("xiaoming 1 3 5", "111")
        assert error is None
        assert game_id_lower == "xiaoming"
        assert display_name == "小明"
        assert indices == [1, 3, 5]

    def test_no_game_id_all_indices_numeric(self):
        self._bind("xiaoming", qq="111", name="小明")
        game_id_lower, display_name, indices, error = br._parse_delete_args("1 3 5", "111")
        assert error is None
        assert game_id_lower == "xiaoming"  # default first binding
        assert indices == [1, 3, 5]

    def test_no_game_id_all_indices_no_bindings(self):
        _, _, _, error = br._parse_delete_args("1 3 5", "111")
        assert "还没有绑定" in error

    def test_game_id_no_indices(self):
        self._bind("xiaoming", qq="111", name="小明")
        _, _, _, error = br._parse_delete_args("xiaoming", "111")
        assert "轮次索引" in error

    def test_exceeds_max_indices(self):
        self._bind("xiaoming", qq="111", name="小明")
        indices_str = " ".join(str(i) for i in range(1, 32))
        _, _, _, error = br._parse_delete_args(f"xiaoming {indices_str}", "111")
        assert "30" in error

    def test_non_numeric_index(self):
        self._bind("xiaoming", qq="111", name="小明")
        _, _, _, error = br._parse_delete_args("xiaoming 1 abc 5", "111")
        assert "不是有效数字" in error

    def test_index_less_than_1(self):
        self._bind("xiaoming", qq="111", name="小明")
        _, _, _, error = br._parse_delete_args("xiaoming 0 3", "111")
        assert "索引从 1 开始" in error


# ============================================================
# Command handler tests
# ============================================================

class TestHandleUpload:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.tmpdir = Path(tempfile.mkdtemp())
        monkeypatch.setattr(b, "DATA_DIR", self.tmpdir)
        monkeypatch.setattr(br, "DATA_DIR", self.tmpdir)
        b._bindings_cache = None
        br._records_cache = None
        self.matcher = AsyncMock()
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        b._bindings_cache = None
        br._records_cache = None

    def _bind(self, game_id, qq="111", name=None):
        data = b._load_bindings()
        if "by_game_id" not in data:
            data["by_game_id"] = {}
        data["by_game_id"][game_id.lower()] = {"qq": qq, "name": name or game_id}
        b._save_bindings(data)

    @pytest.mark.asyncio
    async def test_upload_first_time(self):
        self._bind("xiaoming", qq="111", name="小明")
        await br._handle_upload(self.matcher, "111", "xiaoming 法师 5 3")
        reply = self.matcher.finish.call_args[0][0]
        assert "上传成功" in reply
        assert "法师 5胜3负" in reply
        assert "500" in reply  # 5*100
        assert "均胜" in reply
        assert "5.0" in reply

        data = br._load_records()
        rounds = data["by_game_id"]["xiaoming"]["rounds"]
        assert len(rounds) == 1
        assert rounds[0]["wins"] == 5
        assert rounds[0]["losses"] == 3
        assert rounds[0]["class"] == "法师"
        assert rounds[0]["score"] == 500.0

    @pytest.mark.asyncio
    async def test_upload_appends_to_existing(self):
        self._bind("xiaoming", qq="111", name="小明")
        await br._handle_upload(self.matcher, "111", "xiaoming 法师 5 3")
        await br._handle_upload(self.matcher, "111", "xiaoming 萨满祭司 3 3")

        data = br._load_records()
        rounds = data["by_game_id"]["xiaoming"]["rounds"]
        assert len(rounds) == 2
        assert rounds[0]["score"] == 500.0
        assert rounds[1]["score"] == 750.0  # 500*0.9 + 300

    @pytest.mark.asyncio
    async def test_upload_not_bound(self):
        await br._handle_upload(self.matcher, "111", "nobody 法师 5 3")
        reply = self.matcher.finish.call_args[0][0]
        assert "未绑定" in reply

    @pytest.mark.asyncio
    async def test_upload_invalid_wins(self):
        self._bind("xiaoming", qq="111")
        await br._handle_upload(self.matcher, "111", "xiaoming 法师 13 3")
        reply = self.matcher.finish.call_args[0][0]
        assert "超出合法范围" in reply

    @pytest.mark.asyncio
    async def test_upload_wins_12_losses_0(self):
        self._bind("xiaoming", qq="111", name="小明")
        await br._handle_upload(self.matcher, "111", "xiaoming 法师 12 0")
        reply = self.matcher.finish.call_args[0][0]
        assert "上传成功" in reply
        assert "12胜0负" in reply

        data = br._load_records()
        rounds = data["by_game_id"]["xiaoming"]["rounds"]
        assert rounds[0]["wins"] == 12
        assert rounds[0]["losses"] == 0

    @pytest.mark.asyncio
    async def test_upload_case_insensitive_game_id(self):
        self._bind("XiaoMing", qq="111", name="XiaoMing")
        await br._handle_upload(self.matcher, "111", "xiaoming 法师 5 3")
        reply = self.matcher.finish.call_args[0][0]
        assert "上传成功" in reply
        assert "xiaoming" in br._load_records()["by_game_id"]

    @pytest.mark.asyncio
    async def test_upload_no_game_id_uses_first_binding(self):
        self._bind("xiaoming", qq="111", name="小明")
        await br._handle_upload(self.matcher, "111", "法师 5 3")
        reply = self.matcher.finish.call_args[0][0]
        assert "上传成功" in reply

        data = br._load_records()
        assert "xiaoming" in data["by_game_id"]

    @pytest.mark.asyncio
    async def test_upload_with_alias_dk(self):
        self._bind("xiaoming", qq="111", name="小明")
        await br._handle_upload(self.matcher, "111", "xiaoming dk 12 1")
        reply = self.matcher.finish.call_args[0][0]
        assert "上传成功" in reply
        assert "死亡骑士" in reply

        data = br._load_records()
        assert data["by_game_id"]["xiaoming"]["rounds"][0]["class"] == "死亡骑士"

    @pytest.mark.asyncio
    async def test_upload_invalid_losses(self):
        self._bind("xiaoming", qq="111")
        await br._handle_upload(self.matcher, "111", "xiaoming 法师 5 2")
        reply = self.matcher.finish.call_args[0][0]
        assert "负场必须为3" in reply


class TestHandleMyRecords:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.tmpdir = Path(tempfile.mkdtemp())
        monkeypatch.setattr(b, "DATA_DIR", self.tmpdir)
        monkeypatch.setattr(br, "DATA_DIR", self.tmpdir)
        b._bindings_cache = None
        br._records_cache = None
        self.matcher = AsyncMock()
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        b._bindings_cache = None
        br._records_cache = None

    def _bind(self, game_id, qq="111", name=None):
        data = b._load_bindings()
        if "by_game_id" not in data:
            data["by_game_id"] = {}
        data["by_game_id"][game_id.lower()] = {"qq": qq, "name": name or game_id}
        b._save_bindings(data)

    def _upload(self, game_id, wins, losses, class_name="法师"):
        data = br._load_records()
        game_id_lower = game_id.lower()
        entry = data.get("by_game_id", {}).get(game_id_lower)
        if entry is None:
            entry = {"rounds": []}
            if "by_game_id" not in data:
                data["by_game_id"] = {}
            data["by_game_id"][game_id_lower] = entry
        entry["rounds"].append({"wins": wins, "losses": losses, "class": class_name, "score": 0})
        br._recalc_scores(entry["rounds"])
        br._save_records(data)

    @pytest.mark.asyncio
    async def test_no_bindings(self):
        await br._handle_my_records(self.matcher, "111", "")
        reply = self.matcher.finish.call_args[0][0]
        assert "还没有绑定" in reply

    @pytest.mark.asyncio
    async def test_no_records_yet(self):
        self._bind("xiaoming", qq="111", name="小明")
        await br._handle_my_records(self.matcher, "111", "xiaoming")
        reply = self.matcher.finish.call_args[0][0]
        assert "还没有战绩记录" in reply

    @pytest.mark.asyncio
    async def test_shows_all_rounds(self):
        self._bind("xiaoming", qq="111", name="小明")
        self._upload("xiaoming", 5, 3)
        self._upload("xiaoming", 3, 3, "萨满祭司")
        self._upload("xiaoming", 7, 3, "猎人")
        await br._handle_my_records(self.matcher, "111", "xiaoming")
        reply = self.matcher.finish.call_args[0][0]
        assert "小明" in reply
        assert "第1轮. 法师 5胜3负" in reply
        assert "第2轮. 萨满祭司 3胜3负" in reply
        assert "第3轮. 猎人 7胜3负" in reply
        assert "当前分数" in reply
        assert "均胜" in reply
        assert "1375.0" in reply
        assert "5.0" in reply  # avg of [5,3,7] = 5.0

    @pytest.mark.asyncio
    async def test_empty_input_uses_first_binding(self):
        self._bind("xiaoming", qq="111", name="小明")
        self._bind("xiaohong", qq="111", name="小红")
        self._upload("xiaoming", 5, 3)
        await br._handle_my_records(self.matcher, "111", "")
        reply = self.matcher.finish.call_args[0][0]
        assert "小明" in reply
        assert "小红" not in reply

    @pytest.mark.asyncio
    async def test_game_id_not_bound(self):
        await br._handle_my_records(self.matcher, "111", "nobody")
        reply = self.matcher.finish.call_args[0][0]
        assert "未找到绑定记录" in reply

    @pytest.mark.asyncio
    async def test_game_id_bound_to_other_qq(self):
        self._bind("xiaoming", qq="999", name="小明")
        await br._handle_my_records(self.matcher, "111", "xiaoming")
        reply = self.matcher.finish.call_args[0][0]
        assert "不在你的绑定列表中" in reply

    @pytest.mark.asyncio
    async def test_empty_records_shows_zero_rounds_message(self):
        self._bind("xiaoming", qq="111", name="小明")
        br._save_records({"by_game_id": {"xiaoming": {"rounds": []}}})
        await br._handle_my_records(self.matcher, "111", "xiaoming")
        reply = self.matcher.finish.call_args[0][0]
        assert "还没有战绩记录" in reply


class TestHandleDelete:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.tmpdir = Path(tempfile.mkdtemp())
        monkeypatch.setattr(b, "DATA_DIR", self.tmpdir)
        monkeypatch.setattr(br, "DATA_DIR", self.tmpdir)
        b._bindings_cache = None
        br._records_cache = None
        self.matcher = AsyncMock()
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        b._bindings_cache = None
        br._records_cache = None

    def _bind(self, game_id, qq="111", name=None):
        data = b._load_bindings()
        if "by_game_id" not in data:
            data["by_game_id"] = {}
        data["by_game_id"][game_id.lower()] = {"qq": qq, "name": name or game_id}
        b._save_bindings(data)

    def _upload(self, game_id, wins_list):
        data = br._load_records()
        game_id_lower = game_id.lower()
        entry = data.get("by_game_id", {}).get(game_id_lower)
        if entry is None:
            entry = {"rounds": []}
            if "by_game_id" not in data:
                data["by_game_id"] = {}
            data["by_game_id"][game_id_lower] = entry
        for w in wins_list:
            entry["rounds"].append({"wins": w, "score": 0})
        br._recalc_scores(entry["rounds"])
        br._save_records(data)

    @pytest.mark.asyncio
    async def test_delete_success(self):
        self._bind("xiaoming", qq="111", name="小明")
        self._upload("xiaoming", [5, 3, 7, 2])
        await br._handle_delete(self.matcher, "111", "xiaoming 2 4")
        reply = self.matcher.finish.call_args[0][0]
        assert "已删除" in reply
        assert "2" in reply  # 2 indices deleted
        assert "剩余轮次: 2" in reply

        data = br._load_records()
        rounds = data["by_game_id"]["xiaoming"]["rounds"]
        assert len(rounds) == 2
        assert rounds[0]["wins"] == 5
        assert rounds[1]["wins"] == 7
        # Score should be recalculated
        assert rounds[0]["score"] == 500.0
        assert rounds[1]["score"] == round(500.0 * 0.9 + 7 * 100, 1)  # 1150.0

    @pytest.mark.asyncio
    async def test_delete_no_game_id_uses_first_binding(self):
        self._bind("xiaoming", qq="111", name="小明")
        self._upload("xiaoming", [5, 3, 7])
        await br._handle_delete(self.matcher, "111", "1 3")
        reply = self.matcher.finish.call_args[0][0]
        assert "已删除" in reply

        data = br._load_records()
        rounds = data["by_game_id"]["xiaoming"]["rounds"]
        assert len(rounds) == 1
        assert rounds[0]["wins"] == 3

    @pytest.mark.asyncio
    async def test_delete_index_out_of_range(self):
        self._bind("xiaoming", qq="111", name="小明")
        self._upload("xiaoming", [5, 3])
        await br._handle_delete(self.matcher, "111", "xiaoming 5")
        reply = self.matcher.finish.call_args[0][0]
        assert "超出范围" in reply

    @pytest.mark.asyncio
    async def test_delete_no_records(self):
        self._bind("xiaoming", qq="111", name="小明")
        await br._handle_delete(self.matcher, "111", "xiaoming 1")
        reply = self.matcher.finish.call_args[0][0]
        assert "还没有战绩记录" in reply

    @pytest.mark.asyncio
    async def test_delete_game_id_not_bound(self):
        await br._handle_delete(self.matcher, "111", "nobody 1")
        reply = self.matcher.finish.call_args[0][0]
        assert "未找到绑定记录" in reply

    @pytest.mark.asyncio
    async def test_delete_all_rounds_recalc_empty(self):
        self._bind("xiaoming", qq="111", name="小明")
        self._upload("xiaoming", [5, 3])
        await br._handle_delete(self.matcher, "111", "xiaoming 1 2")
        reply = self.matcher.finish.call_args[0][0]
        assert "已删除" in reply
        assert "剩余轮次: 0" in reply

        data = br._load_records()
        assert data["by_game_id"]["xiaoming"]["rounds"] == []

    @pytest.mark.asyncio
    async def test_delete_duplicate_indices_deduplicated(self):
        self._bind("xiaoming", qq="111", name="小明")
        self._upload("xiaoming", [5, 3, 7, 2])
        await br._handle_delete(self.matcher, "111", "xiaoming 1 1 1")
        reply = self.matcher.finish.call_args[0][0]
        # "1 1 1" → set → {1}, so only 1 deleted
        assert "1" in reply  # 1 round deleted (displayed as number of unique indices)
        data = br._load_records()
        assert len(data["by_game_id"]["xiaoming"]["rounds"]) == 3


# ============================================================
# Integration tests
# ============================================================

class TestIntegration:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.tmpdir = Path(tempfile.mkdtemp())
        monkeypatch.setattr(b, "DATA_DIR", self.tmpdir)
        monkeypatch.setattr(br, "DATA_DIR", self.tmpdir)
        b._bindings_cache = None
        br._records_cache = None
        self.matcher = AsyncMock()
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        b._bindings_cache = None
        br._records_cache = None

    def _bind(self, game_id, qq="111", name=None):
        data = b._load_bindings()
        if "by_game_id" not in data:
            data["by_game_id"] = {}
        data["by_game_id"][game_id.lower()] = {"qq": qq, "name": name or game_id}
        b._save_bindings(data)

    @pytest.mark.asyncio
    async def test_full_upload_my_records_delete_cycle(self):
        self._bind("xiaoming", qq="111", name="小明")

        # Upload multiple rounds
        await br._handle_upload(self.matcher, "111", "xiaoming 法师 5 3")
        await br._handle_upload(self.matcher, "111", "xiaoming 萨满祭司 3 3")
        await br._handle_upload(self.matcher, "111", "xiaoming 猎人 7 3")
        await br._handle_upload(self.matcher, "111", "xiaoming 战士 2 3")

        # My records
        await br._handle_my_records(self.matcher, "111", "xiaoming")
        reply = self.matcher.finish.call_args[0][0]
        assert "第1轮" in reply
        assert "第2轮" in reply
        assert "第3轮" in reply
        assert "第4轮" in reply

        # Delete rounds 2 and 4
        await br._handle_delete(self.matcher, "111", "xiaoming 2 4")
        reply = self.matcher.finish.call_args[0][0]
        assert "已删除" in reply

        # Verify remaining rounds
        await br._handle_my_records(self.matcher, "111", "xiaoming")
        reply = self.matcher.finish.call_args[0][0]
        assert "第1轮. 法师 5胜3负" in reply
        assert "第2轮. 猎人 7胜3负" in reply
        assert "第3轮" not in reply
        assert "第4轮" not in reply

    @pytest.mark.asyncio
    async def test_multi_account_isolation(self):
        self._bind("xiaoming", qq="111", name="小明")
        self._bind("xiaohong", qq="111", name="小红")

        await br._handle_upload(self.matcher, "111", "xiaoming 法师 5 3")
        await br._handle_upload(self.matcher, "111", "xiaohong 萨满祭司 7 3")

        # Each account has independent records
        await br._handle_my_records(self.matcher, "111", "xiaoming")
        reply = self.matcher.finish.call_args[0][0]
        assert "小明" in reply
        assert "第1轮. 法师 5胜3负" in reply
        assert "小红" not in reply

        await br._handle_my_records(self.matcher, "111", "xiaohong")
        reply = self.matcher.finish.call_args[0][0]
        assert "小红" in reply
        assert "第1轮. 萨满祭司 7胜3负" in reply
        assert "小明" not in reply

    @pytest.mark.asyncio
    async def test_disk_persistence(self):
        self._bind("xiaoming", qq="111", name="小明")
        await br._handle_upload(self.matcher, "111", "xiaoming 法师 5 3")

        # Clear caches
        br._records_cache = None

        data = br._load_records()
        assert "xiaoming" in data["by_game_id"]
        assert len(data["by_game_id"]["xiaoming"]["rounds"]) == 1

    @pytest.mark.asyncio
    async def test_ema_score_consistency_after_delete(self):
        """Verify EMA scores are correctly recalculated after deletion."""
        self._bind("xiaoming", qq="111", name="小明")
        await br._handle_upload(self.matcher, "111", "xiaoming 法师 10 3")
        await br._handle_upload(self.matcher, "111", "xiaoming 萨满祭司 10 3")
        await br._handle_upload(self.matcher, "111", "xiaoming 猎人 10 3")

        # Delete middle round
        await br._handle_delete(self.matcher, "111", "xiaoming 2")

        data = br._load_records()
        rounds = data["by_game_id"]["xiaoming"]["rounds"]
        assert len(rounds) == 2
        # Round 1: 10 wins, score = 1000
        assert rounds[0]["score"] == 1000.0
        # Round 2 (was round 3): score = 1000 * 0.9 + 1000 = 1900.0
        assert rounds[1]["score"] == 1900.0

    @pytest.mark.asyncio
    async def test_career_auto_fill_from_binding(self):
        """Verify that when rank command has no args, it auto-fills from binding."""
        # This is tested via query.py's integration, but we verify the binding check works
        self._bind("xiaoming", qq="111", name="小明")
        bindings = b._get_bindings_for_qq("111")
        assert len(bindings) == 1
        assert bindings[0]["name"] == "小明"


# ============================================================
# Any-player PK (PK A B / PK B)
# ============================================================

class TestHandleAnyPk:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.tmpdir = Path(tempfile.mkdtemp())
        monkeypatch.setattr(b, "DATA_DIR", self.tmpdir)
        monkeypatch.setattr(br, "DATA_DIR", self.tmpdir)
        b._bindings_cache = None
        br._records_cache = None
        self.matcher = AsyncMock()
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        b._bindings_cache = None
        br._records_cache = None

    def _bind(self, game_id, qq="111", name=None):
        data = b._load_bindings()
        data.setdefault("by_game_id", {})[game_id.lower()] = {
            "qq": qq, "name": name or game_id,
        }
        b._save_bindings(data)

    def _upload(self, game_id, wins_list):
        data = br._load_records()
        entry = data.setdefault("by_game_id", {}).setdefault(game_id.lower(), {"rounds": []})
        for w in wins_list:
            entry["rounds"].append({"wins": w, "losses": 3, "class": "法师", "score": 0})
        br._recalc_scores(entry["rounds"])
        br._save_records(data)

    @pytest.mark.asyncio
    async def test_a_chasing_b(self):
        self._upload("a", [5])
        self._upload("b", [10])
        await br._handle_any_pk(self.matcher, "111", "a b")
        reply = self.matcher.finish.call_args[0][0]
        assert "【PK · 最新赛季】" in reply
        assert "目标【b】" in reply
        assert "挑战【a】" in reply
        # needed = (1000 - 500*0.9)/100 = 5.5 -> 6 wins
        assert "6胜及以上" in reply

    @pytest.mark.asyncio
    async def test_a_already_ahead(self):
        self._upload("a", [10])
        self._upload("b", [5])
        await br._handle_any_pk(self.matcher, "111", "a b")
        reply = self.matcher.finish.call_args[0][0]
        assert "【a】已经战胜了【b】" in reply

    @pytest.mark.asyncio
    async def test_single_arg_uses_first_binding_as_a(self):
        self._bind("xiaoming", qq="111", name="小明")
        self._upload("xiaoming", [5])
        self._upload("b", [10])
        await br._handle_any_pk(self.matcher, "111", "b")
        reply = self.matcher.finish.call_args[0][0]
        assert "挑战【小明】" in reply
        assert "目标【b】" in reply

    @pytest.mark.asyncio
    async def test_single_arg_without_binding(self):
        await br._handle_any_pk(self.matcher, "111", "b")
        reply = self.matcher.finish.call_args[0][0]
        assert "还没有绑定任何游戏账号" in reply

    @pytest.mark.asyncio
    async def test_missing_record(self):
        self._upload("a", [5])
        await br._handle_any_pk(self.matcher, "111", "a b")
        reply = self.matcher.finish.call_args[0][0]
        assert "【b】还没有战绩记录" in reply

    @pytest.mark.asyncio
    async def test_too_many_args(self):
        await br._handle_any_pk(self.matcher, "111", "a b c")
        reply = self.matcher.finish.call_args[0][0]
        assert "PK 用法" in reply

    @pytest.mark.asyncio
    async def test_same_account(self):
        self._upload("a", [5])
        await br._handle_any_pk(self.matcher, "111", "a A")
        reply = self.matcher.finish.call_args[0][0]
        assert "同一个账号" in reply

    @pytest.mark.asyncio
    async def test_lowercase_pk_goes_to_any_pk(self):
        self._upload("a", [5])
        self._upload("b", [10])
        # "pk A B" is now registered on the PK command, not 与菜逼PK.
        await br._handle_any_pk(self.matcher, "111", "a b")
        reply = self.matcher.finish.call_args[0][0]
        assert "【PK · 最新赛季】" in reply
        assert "目标【b】" in reply
        assert "6胜及以上" in reply
