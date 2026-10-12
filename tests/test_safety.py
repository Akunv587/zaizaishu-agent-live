"""敏感词两级过滤的单元测试（PROMPTS 步骤 4.1）。

五类用例：命中、大小写不敏感、不命中、注释行忽略、fallback 替换；
另覆盖 stats 的 blocked_output 计数与 from_config 加载失败路径。
"""
import pytest

from hub.safety import SafetyError, SafetyFilter, load_words
from hub.stats import StatsCollector


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


WORDS = ["政治", "赌博", "WAIgua"]


def make_filter(words=WORDS, fallback="哈哈这个咱们直播间聊哈", stats=None,
                enabled=True):
    return SafetyFilter(words, fallback_reply=fallback, stats=stats,
                        fallback_enabled=enabled)


def test_sanitize_output_disabled_returns_empty():
    """兜底口播关闭：命中敏感词返回空串（调用方静默），不命中照常。"""
    flt = make_filter(enabled=False)
    assert flt.sanitize_output("这里提到政治") == ""
    assert flt.sanitize_output("正常内容") == "正常内容"


def test_check_input_hit():
    assert make_filter().check_input("这个游戏有没有开挂政治") is True


def test_check_input_case_insensitive():
    assert make_filter().check_input("求个wAiGUA教程") is True


def test_check_input_no_hit():
    assert make_filter().check_input("主播这游戏叫什么呀") is False
    assert make_filter().check_input("") is False


def test_check_input_is_pure_predicate_no_stats():
    stats = StatsCollector()
    make_filter(stats=stats).check_input("政治话题")
    assert stats.get("blocked_input") == 0  # 计数由 Router 负责，谓词本身不计数


def test_load_words_ignores_comments_and_blank(tmp_path):
    path = tmp_path / "words.txt"
    path.write_text("# 注释行\n政治\n\n  赌博  \n#另一条注释\n外挂\n", encoding="utf-8")
    assert load_words(path) == ["政治", "赌博", "外挂"]


def test_load_words_strips_bom(tmp_path):
    # Windows 编辑器常给 UTF-8 文件加 BOM：首行注释不得被读成词条
    path = tmp_path / "words.txt"
    path.write_bytes("# 注释行\n政治\n赌博\n".encode("utf-8-sig"))
    assert load_words(path) == ["政治", "赌博"]


def test_load_words_missing_file_raises(tmp_path):
    with pytest.raises(SafetyError):
        load_words(tmp_path / "nope.txt")


def test_sanitize_output_replaced_with_fallback():
    result = make_filter().sanitize_output("这游戏开赌博玩法是真的爽")
    assert result == "哈哈这个咱们直播间聊哈"


def test_sanitize_output_case_insensitive_and_custom_fallback():
    result = make_filter().sanitize_output("玩WaiGua的兄弟看过来", fallback="换一条安全话术")
    assert result == "换一条安全话术"


def test_sanitize_output_clean_text_unchanged():
    text = "这游戏超好玩兄弟们"
    assert make_filter().sanitize_output(text) == text


def test_sanitize_output_counts_blocked():
    stats = StatsCollector()
    flt = make_filter(stats=stats)
    flt.sanitize_output("涉及政治的话题")
    flt.sanitize_output("正常话术")
    flt.sanitize_output("赌博内容")
    assert stats.get("blocked_output") == 2


def test_from_config_loads_word_file(tmp_path):
    path = tmp_path / "words.txt"
    path.write_text("# 词库\n政治\n\n", encoding="utf-8")
    cfg = {"safety": {"block_words_file": str(path), "fallback_reply": "兜底话术"}}
    flt = SafetyFilter.from_config(cfg)
    assert flt.check_input("聊点政治") is True
    assert flt.sanitize_output("政治") == "兜底话术"


def test_from_config_empty_file_raises(tmp_path):
    path = tmp_path / "words.txt"
    path.write_text("# 只有注释\n", encoding="utf-8")
    cfg = {"safety": {"block_words_file": str(path)}}
    with pytest.raises(SafetyError):
        SafetyFilter.from_config(cfg)
