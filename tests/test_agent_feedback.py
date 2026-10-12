"""反馈进化存储层的单元测试（M6-1）。

验收口径（V6.1 的单元层）：
- 采纳/弃用/改写三路都落 `examples.jsonl` + `feedback.jsonl` 两份，记录形状对齐
  `L2Retriever.index()` 的 examples 契约（`reply` + `event.content`）；
- `rejected` 只留档不进 few-shot；`examples()` 只返 adopted/rewritten 且超限取最新；
- 弃用原因只认四个标签，未知标签丢弃；空原因空备注拒绝；
- 写失败抛 `FeedbackError`（旁路观测不反噬链路，但用户要知道）；
- **接缝实测**：FeedbackStore 的产出直接喂 `L2Retriever.index(examples=...)`
  能索引、能召回——不喂理想形状，喂真产物。
"""
import json

import pytest

from hub.agent import feedback as feedback_mod
from hub.agent.feedback import (
    FEEDBACK_FILE,
    EXAMPLES_FILE,
    MAX_EXAMPLES,
    REASONS,
    SOURCE_ADOPTED,
    SOURCE_REJECTED,
    SOURCE_REWRITTEN,
    FeedbackError,
    FeedbackStore,
    valid_id,
)
from hub.agent.retriever import L2Retriever


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


class Clock:
    def __init__(self, start=1758000000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def store(tmp_path):
    return FeedbackStore(root=tmp_path, clock=Clock())


@pytest.fixture
def entry():
    """页面传来的那条 AI 回复上下文（webui play_start entry 的形状）。"""
    return {
        "event_type": "chat",
        "user": "小明",
        "content": "主播玩什么游戏",
        "event_line": "chat 主播玩什么游戏",
        "reply": "玩地铁跑酷，宝子们猜我今晚上几分",
        "tone": "happy",
        "reason": "命中游戏话题",
    }


# ---------------------------------------------------------------- 写：采纳


def test_adopt_writes_both_files_with_contract_shape(tmp_path, store, entry):
    rec = store.adopt(entry)

    assert rec["source"] == SOURCE_ADOPTED
    assert rec["reply"] == entry["reply"]
    assert rec["event"] == {"content": "主播玩什么游戏"}
    assert rec["user"] == "小明"
    assert rec["tone"] == "happy"
    assert valid_id(rec["id"])
    assert rec["original_reply"] == ""
    assert rec["reasons"] == []

    examples = json.loads(
        (tmp_path / "default" / "agent" / EXAMPLES_FILE).read_text(encoding="utf-8"))
    audit = json.loads(
        (tmp_path / "default" / "agent" / FEEDBACK_FILE).read_text(encoding="utf-8"))
    assert examples["reply"] == entry["reply"]
    assert audit["kind"] == "adopt"
    assert audit["entry"]["content"] == "主播玩什么游戏"      # 原始口径另留一份
    assert audit["reply"] == entry["reply"]


def test_adopt_without_reply_raises(entry, tmp_path):
    store = FeedbackStore(root=tmp_path, clock=Clock())
    bad = dict(entry, reply="  \n ")
    with pytest.raises(FeedbackError):
        store.adopt(bad)


def test_gift_entry_falls_back_content_to_name_x_count(tmp_path):
    store = FeedbackStore(root=tmp_path, clock=Clock())
    rec = store.adopt({
        "event_type": "gift",
        "user": "大明",
        "reply": "谢谢大哥的火箭！",
        "gift_name": "火箭",
        "gift_count": 3,
    })
    assert rec["event"] == {"content": "火箭 ×3"}
    assert rec["event_type"] == "gift"


def test_long_fields_are_clipped(tmp_path):
    store = FeedbackStore(root=tmp_path, clock=Clock())
    rec = store.adopt({
        "event_type": "chat",
        "user": "u" * 100,
        "content": "弹" * 500,
        "reply": "回" * 500,
    })
    assert rec["user"] == "u" * 40
    assert len(rec["event"]["content"]) == 200
    assert len(rec["reply"]) == 200


# ---------------------------------------------------------------- 写：弃用


def test_reject_keeps_four_known_reasons_and_drops_unknown(store, entry):
    rec = store.reject(entry, ["太长", "不存在的原因", "重复", "太长"])
    assert rec["source"] == SOURCE_REJECTED
    assert rec["reasons"] == ["太长", "重复"]


def test_reject_accepts_single_reason_string(store, entry):
    rec = store.reject(entry, "跑题")
    assert rec["reasons"] == ["跑题"]


def test_reject_without_reason_or_note_raises(store, entry):
    with pytest.raises(FeedbackError):
        store.reject(entry, [])
    with pytest.raises(FeedbackError):
        store.reject(entry, ["乱写的"])


def test_reject_with_note_only_is_allowed(store, entry):
    rec = store.reject(entry, [], note="今天嗓子哑，这句不像我")
    assert rec["reasons"] == []
    assert rec["note"] == "今天嗓子哑，这句不像我"


# ---------------------------------------------------------------- 写：改写


def test_rewrite_stores_new_text_keeps_original(store, entry):
    rec = store.rewrite(entry, "改后的回复：地铁跑酷，来猜猜我几分")
    assert rec["source"] == SOURCE_REWRITTEN
    assert rec["reply"] == "改后的回复：地铁跑酷，来猜猜我几分"
    assert rec["original_reply"] == entry["reply"]


def test_rewrite_with_empty_text_raises(store, entry):
    with pytest.raises(FeedbackError):
        store.rewrite(entry, "   ")


def test_adopt_keeps_original_reply_empty(store, entry):
    rec = store.adopt(entry)
    assert rec["original_reply"] == ""


# ---------------------------------------------------------------- 读：few-shot


def test_examples_excludes_rejected_and_empty_reply(tmp_path, store, entry):
    store.adopt(entry)
    store.rewrite(entry, "改写版")
    store.reject(entry, ["太长"])
    # 手工塞一条空回复的脏样本
    path = tmp_path / "default" / "agent" / EXAMPLES_FILE
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": "fb_x1", "source": "adopted", "reply": "  ",
                            "event": {"content": "x"}}) + "\n")

    ex = store.examples()
    assert len(ex) == 2
    assert {e["source"] for e in ex} == {SOURCE_ADOPTED, SOURCE_REWRITTEN}


def test_examples_keeps_newest_when_over_limit(tmp_path, store, entry, monkeypatch):
    monkeypatch.setattr(feedback_mod, "MAX_EXAMPLES", 3)
    replies = []
    for i in range(5):
        rec = store.adopt(dict(entry, reply=f"回复{i}"))
        replies.append(rec["reply"])
    got = [e["reply"] for e in store.examples()]
    assert got == replies[-3:]          # 超限取最新，最旧的被拒之门外


def test_read_skips_dirty_lines(tmp_path, store, entry):
    store.adopt(entry)
    path = tmp_path / "default" / "agent" / EXAMPLES_FILE
    raw = path.read_text(encoding="utf-8")
    path.write_text("不是json\n" + raw + '{"broken": \n' + '"str_entry"\n',
                    encoding="utf-8")
    assert len(store.all_examples()) == 1
    assert len(store.examples()) == 1


def test_read_missing_file_returns_empty(tmp_path):
    store = FeedbackStore(root=tmp_path, clock=Clock())
    assert store.all_examples() == []
    assert store.examples() == []


# ---------------------------------------------------------------- 统计


def test_stats_counts_sources_week_and_reasons(tmp_path, store, entry):
    store.adopt(entry)
    store.adopt(dict(entry, reply="第二条"))
    store.rewrite(entry, "改写版")
    store.reject(entry, ["太长", "重复"])

    s = store.stats()
    assert s["total"] == 4
    assert s["adopted"] == 2
    assert s["rewritten"] == 1
    assert s["rejected"] == 1
    assert s["usable"] == 3
    # 采纳率 = adopted / (adopted + rejected)，改写不折进分母
    assert s["adopt_rate"] == 66.7
    assert s["reasons"] == {"太长": 1, "重复": 1}
    assert s["week_added"] == 4
    assert s["max_examples"] == MAX_EXAMPLES


def test_stats_week_window_uses_clock(tmp_path):
    clock = Clock(1758000000.0)
    store = FeedbackStore(root=tmp_path, clock=clock)
    path = tmp_path / "default" / "agent" / EXAMPLES_FILE
    path.parent.mkdir(parents=True)
    from datetime import datetime
    old = datetime.fromtimestamp(clock() - 30 * 86400).isoformat()
    recent = datetime.fromtimestamp(clock() - 86400).isoformat()
    path.write_text(
        json.dumps({"id": "fb_old", "source": "adopted", "reply": "旧",
                    "ts": old, "reasons": []}) + "\n" +
        json.dumps({"id": "fb_new", "source": "adopted", "reply": "新",
                    "ts": recent, "reasons": []}) + "\n",
        encoding="utf-8")
    s = store.stats()
    assert s["week_added"] == 1
    assert s["adopt_rate"] == 100.0


def test_stats_empty_store(tmp_path):
    store = FeedbackStore(root=tmp_path, clock=Clock())
    s = store.stats()
    assert s["total"] == 0 and s["adopt_rate"] == 0.0 and s["week_added"] == 0


# ---------------------------------------------------------------- 存档隔离


def test_pid_provider_switches_directories(tmp_path, entry):
    pid = {"v": "p_aaa"}
    store = FeedbackStore(root=tmp_path, pid_provider=lambda: pid["v"], clock=Clock())
    store.adopt(entry)
    assert (tmp_path / "p_aaa" / "agent" / EXAMPLES_FILE).is_file()

    pid["v"] = "p_bbb"                      # 活动存档切换后自动跟随
    assert store.pid == "p_bbb"
    assert store.all_examples() == []
    store.adopt(dict(entry, reply="另一个存档的样本"))
    assert (tmp_path / "p_bbb" / "agent" / EXAMPLES_FILE).is_file()
    assert (tmp_path / "p_aaa" / "agent" / EXAMPLES_FILE).is_file()


def test_pid_provider_crash_falls_back_to_default(tmp_path):
    def boom():
        raise RuntimeError("selector 未就绪")

    store = FeedbackStore(root=tmp_path, pid_provider=boom, clock=Clock())
    assert store.pid == "default"


# ---------------------------------------------------------------- 写失败


def test_write_failure_raises_feedback_error(tmp_path, entry):
    # root 是一个文件：mkdir 必然 OSError → 写失败要显式抛出让 webui 弹提示
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    store = FeedbackStore(root=blocker, clock=Clock())
    with pytest.raises(FeedbackError):
        store.adopt(entry)


# ---------------------------------------------------------------- 接缝实测


def test_store_output_feeds_retriever_and_recalls(tmp_path, store, entry):
    """真产物直接喂 L2Retriever：不转形状、不造夹具。"""
    store.adopt(entry)
    store.rewrite(entry, "改写版：地铁跑酷上分教学")
    store.reject(entry, ["太长"])           # 不应进索引

    rt = L2Retriever(top_k=3, clock=Clock())
    rt.index({"samples_ai": [], "topics_ai": []}, examples=store.examples())

    kinds = {e["kind"] for e in rt._entries}
    assert kinds == {"example"}
    assert rt.size == 2

    hits = rt.retrieve("主播玩什么游戏")
    assert hits, "采纳的样本应被召回"
    assert hits[0]["reply"] == entry["reply"]
    assert rt.entry_item(hits[0]["id"])["source"] == SOURCE_ADOPTED


def test_reasons_constant_matches_design():
    assert REASONS == ("不像主播", "太长", "跑题", "重复")
