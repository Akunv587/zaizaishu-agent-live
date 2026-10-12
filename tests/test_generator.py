"""Generator LLM 客户端的单元测试（PROMPTS 步骤 3.1）。

不用网络：注入伪造 client_factory（返回可控的假 httpx 客户端），
覆盖成功、两次失败降级到兜底、首次失败重试成功三条路径。
"""
import asyncio

import pytest

from hub.generator import Generator
from hub.stats import StatsCollector


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeClient:
    """按共享 behaviors 队列依次返回/抛错；多次构造共享同一队列（重试语义）。"""

    def __init__(self, behaviors):
        self._behaviors = behaviors

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        self.captured = {"url": url, "json": json, "headers": headers}
        behavior = self._behaviors.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


def ok_response(text="这是回复话术"):
    return FakeResponse(payload={"choices": [{"message": {"content": text}}]})


def make_generator(behaviors, stats=None):
    clients = []

    def factory():
        c = FakeClient(behaviors)
        clients.append(c)
        return c

    cfg = {
        "llm": {"base_url": "https://api.example.com/v1", "api_key": "sk-test",
                "model": "test-model", "temperature": 0.8, "max_tokens": 120,
                "timeout_seconds": 8},
        "safety": {"fallback_reply": "哈哈这个咱们直播间聊哈"},
    }
    gen = Generator(config=cfg, stats=stats, client_factory=factory, system_prompt="测试人设")
    return gen, clients


def test_success_returns_text():
    behaviors = [ok_response("主播玩的是超好玩的游戏哈")]
    gen, clients = make_generator(behaviors)
    result = asyncio.run(gen.generate_reply({
        "type": "chat", "user": "甲", "content": "这是什么游戏",
        "merged_count": 1, "members": [("甲", "这是什么游戏")],
    }))
    assert result == "主播玩的是超好玩的游戏哈"
    # 请求形态核对：URL 正斜杠、模型、messages
    req = clients[0].captured
    assert req["url"] == "https://api.example.com/v1/chat/completions"
    assert req["json"]["model"] == "test-model"
    assert req["json"]["temperature"] == 0.8
    assert req["json"]["max_tokens"] == 120
    assert req["json"]["messages"][0] == {"role": "system", "content": "测试人设"}
    assert req["json"]["messages"][1]["content"] == "甲：这是什么游戏"
    assert req["headers"]["Authorization"] == "Bearer sk-test"


def test_merged_group_user_message_lines():
    behaviors = [ok_response("一条回复覆盖整组")]
    gen, _ = make_generator(behaviors)
    ctx = {"type": "chat", "user": "甲", "content": "这游戏好玩吗", "merged_count": 3,
           "members": [("甲", "这游戏好玩吗"), ("乙", "这游戏好玩吗？"), ("丙", "好玩吗这游戏")]}
    asyncio.run(gen.generate_reply(ctx))
    assert gen.build_user_message(ctx) == "甲：这游戏好玩吗\n乙：这游戏好玩吗？\n丙：好玩吗这游戏"


def test_timeout_both_attempts_fallback():
    import httpx
    stats = StatsCollector()
    behaviors = [httpx.TimeoutException("超时"), httpx.TimeoutException("又超时")]
    gen, _ = make_generator(behaviors, stats=stats)
    result = asyncio.run(gen.generate_reply({"user": "甲", "content": "x", "members": [("甲", "x")]}))
    assert result == "哈哈这个咱们直播间聊哈"
    assert stats.get("llm_fail") == 2


def test_fallback_disabled_returns_none():
    """兜底口播关闭：生成两次都失败时返回 None（调用方静默），不播兜底文案。"""
    import httpx
    stats = StatsCollector()
    behaviors = [httpx.TimeoutException("超时"), httpx.TimeoutException("又超时")]
    gen, _ = make_generator(behaviors, stats=stats)
    gen.set_fallback(enabled=False)
    result = asyncio.run(gen.generate_reply({"user": "甲", "content": "x", "members": [("甲", "x")]}))
    assert result is None
    assert stats.get("llm_fail") == 2


def test_set_fallback_text_hot_apply():
    """set_fallback 热替换文案：失败兜底用新文案。"""
    import httpx
    behaviors = [httpx.TimeoutException("超时"), httpx.TimeoutException("超时")]
    gen, _ = make_generator(behaviors)
    gen.set_fallback("先这样吧，兄弟们")
    result = asyncio.run(gen.generate_reply({"user": "甲", "content": "x", "members": [("甲", "x")]}))
    assert result == "先这样吧，兄弟们"


def test_first_timeout_retry_success():
    import httpx
    stats = StatsCollector()
    behaviors = [httpx.TimeoutException("超时"), ok_response("重试成功的话术")]
    gen, _ = make_generator(behaviors, stats=stats)
    result = asyncio.run(gen.generate_reply({"user": "甲", "content": "x", "members": [("甲", "x")]}))
    assert result == "重试成功的话术"
    assert stats.get("llm_fail") == 1


def test_non_200_falls_back():
    stats = StatsCollector()
    behaviors = [FakeResponse(status_code=503, text="服务不可用"),
                 FakeResponse(status_code=503, text="服务不可用")]
    gen, _ = make_generator(behaviors, stats=stats)
    result = asyncio.run(gen.generate_reply({"user": "甲", "content": "x", "members": [("甲", "x")]}))
    assert result == "哈哈这个咱们直播间聊哈"
    assert stats.get("llm_fail") == 2


def test_bad_payload_falls_back():
    stats = StatsCollector()
    behaviors = [FakeResponse(payload={"nope": 1}), FakeResponse(payload={"choices": []})]
    gen, _ = make_generator(behaviors, stats=stats)
    result = asyncio.run(gen.generate_reply({"user": "甲", "content": "x", "members": [("甲", "x")]}))
    assert result == "哈哈这个咱们直播间聊哈"


def test_empty_reply_text_treated_as_failure():
    behaviors = [FakeResponse(payload={"choices": [{"message": {"content": "  "}}]}),
                 ok_response("兜住之后的回复")]
    gen, _ = make_generator(behaviors)
    result = asyncio.run(gen.generate_reply({"user": "甲", "content": "x", "members": [("甲", "x")]}))
    assert result == "兜住之后的回复"


def test_default_system_prompt():
    gen = Generator(config={"llm": {}, "safety": {}})
    assert "抖音游戏主播" in gen.system_prompt
    assert "不超过40字" in gen.system_prompt


# ---------- select_reply（template 方案应答选择，v3.1） ----------

CANDS = [
    {"id": "sample:s1", "kind": "sample", "match": "问下载（弹幕如：怎么下载）",
     "reply": "点小手柄下载"},
    {"id": "topic:t1", "kind": "topic", "match": "配置要求（关键词：配置）",
     "reply": "这游戏不吃配置"},
]


def test_select_returns_chosen_candidate_id():
    stats = StatsCollector()
    behaviors = [ok_response("2")]
    gen, clients = make_generator(behaviors, stats=stats)
    chosen = asyncio.run(gen.select_reply("这游戏卡不卡", CANDS))
    assert chosen == "topic:t1"
    assert stats.get("llm_select") == 1
    # 请求形态：温度 0、max_tokens=64（推理模型预留作答空间）、选择提示词、候选编号入 user 消息
    req = clients[0].captured["json"]
    assert req["temperature"] == 0
    assert req["max_tokens"] == 64
    assert "选择" in req["messages"][0]["content"]
    user_msg = req["messages"][1]["content"]
    assert "这游戏卡不卡" in user_msg
    assert "1. 适用：问下载" in user_msg and "2. 适用：配置要求" in user_msg


def test_select_zero_means_no_match():
    behaviors = [ok_response("0")]
    gen, _ = make_generator(behaviors)
    assert asyncio.run(gen.select_reply("今天吃啥", CANDS)) is None


def test_select_out_of_range_means_no_match():
    behaviors = [ok_response("9")]
    gen, _ = make_generator(behaviors)
    assert asyncio.run(gen.select_reply("怎么下载", CANDS)) is None


def test_select_garbage_means_no_match():
    behaviors = [ok_response("我觉得都挺好")]
    gen, _ = make_generator(behaviors)
    assert asyncio.run(gen.select_reply("怎么下载", CANDS)) is None


def test_select_parses_number_in_prose():
    behaviors = [ok_response("选 1 吧")]
    gen, _ = make_generator(behaviors)
    assert asyncio.run(gen.select_reply("怎么下载", CANDS)) == "sample:s1"


def test_select_failure_raises_after_retry():
    import httpx
    from hub.generator import LLMError
    stats = StatsCollector()
    behaviors = [httpx.TimeoutException("超时"), httpx.TimeoutException("又超时")]
    gen, _ = make_generator(behaviors, stats=stats)
    try:
        asyncio.run(gen.select_reply("怎么下载", CANDS))
        raised = False
    except LLMError:
        raised = True
    assert raised
    assert stats.get("llm_fail") == 2


def test_select_first_timeout_retry_success():
    import httpx
    behaviors = [httpx.TimeoutException("超时"), ok_response("1")]
    gen, _ = make_generator(behaviors)
    assert asyncio.run(gen.select_reply("怎么下载", CANDS)) == "sample:s1"


def test_select_empty_candidates_short_circuit():
    behaviors = []  # 不应发任何请求
    gen, _ = make_generator(behaviors)
    assert asyncio.run(gen.select_reply("怎么下载", [])) is None
