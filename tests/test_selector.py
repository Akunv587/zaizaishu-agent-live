"""话术运行时（v4：单方案纯AI + 存档内多音色）的单元测试。

验收口径：
- reload 兜底：无指针/坏指针 → 自动建默认存档并返回需写回的 id；坏存档 → error 状态
- 音色视图：current_voice / voice_params 按存档 current_voice 与 voices 返回
- 弹幕三级应答：① 关键词直出（命中样本 examples/type_name 直接播，不走 LLM；
  有当前音色音频播音频，无音频/音频过期现场 TTS 念当前文本）；
  ② LLM 选择（只从「有当前音色音频且未过期」的样本里选）；
  ③ 现场生成（无候选/未选中/选择失败 → fallback_replier，永不静默）；
  600s 窗口内播过的候选防复读剔除；未注入 fallback → None
- 多音色解析：音频按存档当前音色解析；切音色后同一条目解析到新音色音频；
  stale（text_hash 不符）视为无音频；无当前音色 → 无候选
- 存档供料：system 提示词（知识库+话题话风参考+AI 样本示例）、事件模板映射、
  秒级轮播条目（只供有当前音色音频的启用话题）
"""
import asyncio
import wave

import pytest

from hub.scriptstore import ScriptStore, text_hash
from hub.selector import ScriptRuntime, fill_reply_fields
from hub.stats import StatsCollector


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


def make_wav_bytes(frames=4800, rate=24000) -> bytes:
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x01\x00" * frames)
    return buf.getvalue()


@pytest.fixture
def store(tmp_path):
    return ScriptStore(root=tmp_path / "profiles")


def make_rt(store, cfg=None, selector=None, stats=None, fallback=None):
    base = {"safety": {"fallback_reply": "哈哈这个咱们直播间聊哈"},
            "persona": {"nickname_prefix_len": 2},
            "script": {"active_profile": ""}}
    if cfg:
        base.update(cfg)
    rt = ScriptRuntime(store, base, stats=stats or StatsCollector())
    rt.set_llm_selector(selector)
    rt.set_fallback_replier(fallback)
    rt.reload()
    return rt


def pick(cid):
    """假 LLM 选择器：固定返回指定候选 id（None=不选）；记录入参供断言。"""
    async def _sel(text, candidates):
        _sel.calls.append({"text": text, "candidates": candidates})
        return cid
    _sel.calls = []
    return _sel


async def boom_selector(text, candidates):
    raise RuntimeError("LLM 挂了")


def gen_text(fixed="现场生成的话术"):
    """假现场生成器（未命中候选时兜底）；记录调用供断言。"""
    async def _gen(context):
        _gen.calls.append(context)
        return fixed
    _gen.calls = []
    return _gen


def pick_first():
    """自适应选择器：总选候选列表第一条（无候选返回 None）；记录入参供断言。"""
    async def _sel(text, candidates):
        _sel.calls.append({"text": text, "candidates": candidates})
        return candidates[0]["id"] if candidates else None
    _sel.calls = []
    return _sel


def build_profile(store, name="测试存档"):
    p = store.create_profile(name)
    return p["id"]


def attach_audio(store, pid, section, item_id, voice_id, text, name="a.wav"):
    """保存一份 wav 并把条目 audios[voice_id] 指向它（text_hash 对齐当前文本）。"""
    fname = store.save_audio(pid, make_wav_bytes(), name)
    p = store.get_profile(pid)
    item = next(x for x in p[section] if x["id"] == item_id)
    item.setdefault("audios", {})[voice_id] = {"file": fname,
                                               "text_hash": text_hash(text)}
    store.save_profile(pid, p)
    return fname


def attach_emotion_audio(store, pid, section, item_id, voice_id, emotion,
                         text, name="e.wav"):
    """把条目「音色 × 情绪档」指向一份新 wav（text_hash 对齐当前文本）。"""
    fname = store.save_audio(pid, make_wav_bytes(), name)
    p = store.get_profile(pid)
    item = next(x for x in p[section] if x["id"] == item_id)
    slots = item.setdefault("audios", {}).setdefault(voice_id, {})
    slots[emotion] = {"file": fname, "text_hash": text_hash(text)}
    store.save_profile(pid, p)
    return fname


# ---------- 字段填充 ----------

def test_fill_reply_fields_shortens_user_and_formats_count():
    out = fill_reply_fields("谢谢{user}的{count}{gift}",
                            {"user": "小明同学", "gift": "玫瑰", "count": 3}, 2)
    assert out == "谢谢小明的3个玫瑰"
    out1 = fill_reply_fields("谢谢{user}", {"user": "甲", "count": 1}, 2)
    assert out1 == "谢谢甲"
    # 未知占位符不炸，原文返回
    assert fill_reply_fields("你好{unknown}", {"user": "甲"}, 2) == "你好{unknown}"


# ---------- reload 兜底 ----------

def test_reload_creates_default_profile_when_unset(store):
    rt = make_rt(store)
    assert rt.profile is not None
    assert rt.profile_id
    assert rt.status()["scheme"] == "ai"


def test_reload_falls_back_when_pointer_broken(store):
    rt = make_rt(store, cfg={"script": {"active_profile": "p_nonexistent"}})
    assert rt.profile is not None
    assert rt.profile_id != "p_nonexistent"


def test_reload_returns_defaulted_pid_only_when_created(store):
    rt = ScriptRuntime(store, {"script": {"active_profile": ""}})
    defaulted = rt.reload()
    assert defaulted == rt.profile_id
    # 调用方把兜底 id 写回 cfg 后，再次 reload 不再兜底
    rt._cfg["script"]["active_profile"] = defaulted
    assert rt.reload() == ""


def test_status_error_on_corrupt_profile(store):
    pid = build_profile(store)
    (store.root / pid / "profile.yaml").write_text("- 这是\n- 列表不是映射", encoding="utf-8")
    rt = make_rt(store, cfg={"script": {"active_profile": pid}})
    assert rt.profile is None
    assert rt.status()["error"]
    assert asyncio.run(rt.ai_replier({"user": "甲", "content": "在吗"})) is None
    assert rt.schedule()["enabled"] is False


# ---------- 存档音色视图 ----------

def test_status_reports_voices_and_current_voice(store):
    pid = build_profile(store)
    store.add_voice(pid, "v1", "标准")
    rt = make_rt(store, cfg={"script": {"active_profile": pid}})
    st = rt.status()
    assert st["voices"] == 1
    assert st["current_voice"] == "v1"      # 首个音色自动成为当前直播音色


def test_current_voice_and_voice_params(store):
    pid = build_profile(store)
    store.add_voice(pid, "v1", "标准")
    store.add_voice(pid, "v2", "情绪")
    store.update_voice(pid, "v2", params={"speed": 1.2, "emotion": "happy"})
    rt = make_rt(store, cfg={"script": {"active_profile": pid}})
    assert rt.current_voice() == "v1"
    assert rt.voice_params() == {}                       # 默认=当前音色（v1 无参数）
    assert rt.voice_params("v2") == {"speed": 1.2, "emotion": "happy"}
    store.set_current_voice(pid, "v2")
    rt.reload()
    assert rt.current_voice() == "v2"
    assert rt.voice_params() == {"speed": 1.2, "emotion": "happy"}


# ---------- 存档供料 ----------

def test_build_system_prompt_uses_knowledge_and_samples(store):
    pid = build_profile(store)
    p = store.get_profile(pid)
    p["knowledge"].update({"nickname": "仔仔鼠", "game_name": "超级游戏",
                           "game_intro": "一句话亮点", "download_hint": "点手柄",
                           "highlights": ["卖点一"]})
    p["samples_ai"] = [{"id": "s1", "type_name": "问下载", "examples": ["怎么下载"],
                        "reply_text": "点手柄下载", "audios": {}, "enabled": True}]
    store.save_profile(pid, p)
    rt = make_rt(store, cfg={"script": {"active_profile": pid}})

    prompt = rt.build_system_prompt({})
    assert "仔仔鼠" in prompt and "超级游戏" in prompt and "一句话亮点" in prompt
    assert "弹幕「怎么下载」→ 回复「点手柄下载」" in prompt
    assert rt.max_reply_chars() == 40


def test_build_system_prompt_injects_topic_style_ref(store):
    """话题文本作为话风参考注入提示词（话题不作回复候选）。"""
    pid = build_profile(store)
    p = store.get_profile(pid)
    p["topics_ai"] = [{"id": "t1", "title": "下载引导", "text": "兄弟们点个小手柄就能免费下载",
                       "audios": {}, "enabled": True}]
    store.save_profile(pid, p)
    rt = make_rt(store, cfg={"script": {"active_profile": pid}})
    prompt = rt.build_system_prompt({})
    assert "直播间话题与话风参考" in prompt
    assert "兄弟们点个小手柄" in prompt


def test_build_system_prompt_falls_back_to_base_cfg_without_profile(store):
    pid = build_profile(store)
    (store.root / pid / "profile.yaml").write_text("x: [", encoding="utf-8")
    rt = make_rt(store, cfg={"script": {"active_profile": pid},
                             "persona": {"nickname": "阿宝", "nickname_prefix_len": 2}})
    prompt = rt.build_system_prompt({"persona": {"nickname": "阿宝"}, "game": {}})
    assert "阿宝" in prompt


def test_event_templates_mapping(store):
    pid = build_profile(store)
    p = store.get_profile(pid)
    p["event_templates"]["gift_low"] = ["谢{user}的礼物"]
    store.save_profile(pid, p)
    rt = make_rt(store, cfg={"script": {"active_profile": pid}})
    mapping = rt.event_templates()
    assert mapping["gift_low"] == ["谢{user}的礼物"]
    assert "gift_high" in mapping and "follow" in mapping


def test_event_enabled_mapping(store):
    pid = build_profile(store)
    p = store.get_profile(pid)
    p["event_enabled"]["enter"] = True
    store.save_profile(pid, p)
    rt = make_rt(store, cfg={"script": {"active_profile": pid}})
    enabled = rt.event_enabled()
    assert enabled["enter"] is True
    assert enabled["gift_low"] is True
    assert set(enabled) == {"gift_low", "gift_mid", "gift_high",
                            "follow", "fansclub", "enter"}


# ---------- 轮播调度供料（只供有当前音色音频的话题） ----------

def test_schedule_items_enabled_topics_with_audio(store):
    pid = build_profile(store)
    store.add_voice(pid, "v1", "标准")
    p = store.get_profile(pid)
    p["schedule"] = {"enabled": True, "interval_s": 45, "jitter_s": 5}
    p["topics_ai"] = [
        {"id": "t1", "title": "话题一", "text": "文本一", "audios": {}, "enabled": True},
        {"id": "t2", "title": "话题二", "text": "文本二", "audios": {}, "enabled": False},
        {"id": "t3", "title": "空话题", "text": "", "audios": {}, "enabled": True},
    ]
    store.save_profile(pid, p)
    audio_name = attach_audio(store, pid, "topics_ai", "t1", "v1", "文本一")
    rt = make_rt(store, cfg={"script": {"active_profile": pid}})

    sc = rt.schedule()
    assert sc["enabled"] is True and sc["interval_s"] == 45 and sc["jitter_s"] == 5
    assert len(sc["items"]) == 1   # 禁用/空文本/无音频话题剔除
    assert sc["items"][0]["text"] == "文本一"
    assert sc["items"][0]["audio_path"].endswith(audio_name)


def test_schedule_skips_stale_and_other_voice_audio(store):
    """音频过期（文本已改）与仅挂在别的音色名下的音频都不进轮播。"""
    pid = build_profile(store)
    store.add_voice(pid, "v1", "标准")
    store.add_voice(pid, "v2", "情绪")
    p = store.get_profile(pid)
    p["topics_ai"] = [
        {"id": "t1", "title": "新文本话题", "text": "改过的新文本", "audios": {},
         "enabled": True},
        {"id": "t2", "title": "只有v2音频", "text": "文本二", "audios": {},
         "enabled": True},
    ]
    store.save_profile(pid, p)
    # t1 的音频对应旧文本 → text_hash 不符 = stale
    attach_audio(store, pid, "topics_ai", "t1", "v1", "旧文本")
    # t2 只有 v2 的音频，当前音色是 v1
    attach_audio(store, pid, "topics_ai", "t2", "v2", "文本二")
    rt = make_rt(store, cfg={"script": {"active_profile": pid}})
    assert rt.current_voice() == "v1"
    assert rt.schedule()["items"] == []
    # 切到 v2 后 t2 可播、t1 仍 stale
    store.set_current_voice(pid, "v2")
    rt.reload()
    items = rt.schedule()["items"]
    assert [i["text"] for i in items] == ["文本二"]


# ---------- 情绪档三维回落（T3） ----------

def _emotion_profile(store):
    """音色 v1（当前）+ 一条话题 + 一条样本，各带 default 档音频；话题另有 happy 档。"""
    pid = build_profile(store)
    store.add_voice(pid, "v1", "标准")
    p = store.get_profile(pid)
    p["topics_ai"] = [{"id": "t1", "title": "话题", "text": "正文",
                       "audios": {}, "enabled": True}]
    p["samples_ai"] = [{"id": "s1", "type_name": "样本", "reply_text": "回复",
                        "audios": {}, "enabled": True}]
    store.save_profile(pid, p)
    d_topic = attach_emotion_audio(store, pid, "topics_ai", "t1", "v1", "default", "正文")
    h_topic = attach_emotion_audio(store, pid, "topics_ai", "t1", "v1", "happy", "正文")
    d_sample = attach_emotion_audio(store, pid, "samples_ai", "s1", "v1", "default", "回复")
    return pid, d_topic, h_topic, d_sample


def test_resolve_audio_emotion_three_levels(store):
    """① 命中请求档播预录；② 档位已登记但缺音频 → 回落 default；③ 未登记档 → 空串（现场 TTS）。"""
    pid, d_topic, h_topic, d_sample = _emotion_profile(store)
    rt = make_rt(store, cfg={"script": {"active_profile": pid}})
    assert rt.current_emotions() == ["default", "happy", "calm"]
    item_t = rt.profile["topics_ai"][0]
    item_s = rt.profile["samples_ai"][0]

    # ① 命中请求档（缺省 emotion = default）
    assert rt._resolve_audio(item_t, emotion="happy").endswith(h_topic)
    assert rt._resolve_audio(item_t).endswith(d_topic)
    assert rt._resolve_audio(item_t, emotion="default").endswith(d_topic)
    # ② 已登记（calm 在档位集合内）但缺音频 → 回落该音色 default 预录
    assert rt._resolve_audio(item_t, emotion="calm").endswith(d_topic)
    assert rt._resolve_audio(item_s, emotion="calm").endswith(d_sample)
    # ③ 未登记档位（sad 不在集合）→ 空串，交给播放侧现场 TTS
    assert rt._resolve_audio(item_t, emotion="sad") == ""
    assert rt._resolve_audio(item_s, emotion="sad") == ""
    # 非法档位字符串按 default 处理（不抛错、不空转）
    assert rt._resolve_audio(item_t, emotion="吼叫").endswith(d_topic)


def test_resolve_audio_emotion_stale_and_missing_file(store):
    """改文本/删文件都会让该档失效；改文本时所有档位一起失效。"""
    pid, d_topic, h_topic, d_sample = _emotion_profile(store)
    rt = make_rt(store, cfg={"script": {"active_profile": pid}})
    item_t = rt.profile["topics_ai"][0]
    store.delete_audio(pid, h_topic)
    assert rt._resolve_audio(item_t, emotion="happy").endswith(d_topic)  # 文件没了 → 回落
    p = store.get_profile(pid)
    next(x for x in p["topics_ai"] if x["id"] == "t1")["text"] = "改过的正文"
    store.save_profile(pid, p)
    rt.reload()
    item_t = rt.profile["topics_ai"][0]
    assert rt._resolve_audio(item_t, emotion="happy") == ""
    assert rt._resolve_audio(item_t) == ""       # default 档同样 stale


def test_schedule_uses_default_emotion(store):
    """轮播（话题口播）恒走 default 档音频。"""
    pid, d_topic, h_topic, d_sample = _emotion_profile(store)
    p = store.get_profile(pid)
    p["schedule"] = {"enabled": True, "interval_s": 30, "jitter_s": 0}
    store.save_profile(pid, p)
    rt = make_rt(store, cfg={"script": {"active_profile": pid}})
    items = rt.schedule()["items"]
    assert [i["text"] for i in items] == ["正文"]
    assert items[0]["audio_path"].endswith(d_topic)


# ---------- 弹幕三级应答 ----------

def _ai_profile(store, with_audio=True):
    """AI 存档：音色 v1（自动成为当前）+ 一个样本 + 一个话题（可选带 v1 音频）。"""
    pid = store.create_profile("AI档")["id"]
    store.add_voice(pid, "v1", "标准")
    p = store.get_profile(pid)
    p["samples_ai"] = [{"id": "s1", "type_name": "问下载", "examples": ["怎么下载"],
                        "reply_text": "点手柄下载", "audios": {}, "enabled": True}]
    p["topics_ai"] = [{"id": "t1", "title": "下载引导", "text": "点点小手柄就能下载",
                       "audios": {}, "enabled": True}]
    store.save_profile(pid, p)
    audio_name = ""
    if with_audio:
        audio_name = attach_audio(store, pid, "samples_ai", "s1", "v1", "点手柄下载")
        attach_audio(store, pid, "topics_ai", "t1", "v1", "点点小手柄就能下载")
    return pid, audio_name


def test_ai_llm_select_plays_selected_audio(store):
    """LLM 选择（关键词未命中时的兜底）：选中已有当前音色音频的样本 → 直接播该音频。"""
    pid, audio_name = _ai_profile(store)
    gen = gen_text()
    sel = pick("sample:s1")
    rt = make_rt(store, cfg={"script": {"active_profile": pid}},
                 selector=sel, fallback=gen)

    reply = asyncio.run(rt.ai_replier({"user": "甲", "content": "怎么安装"}))
    assert reply["text"] == "点手柄下载"
    assert reply["audio_path"].endswith(audio_name)
    assert gen.calls == []                          # 未降级到现场生成
    cands = sel.calls[0]["candidates"]
    assert cands[0]["id"] == "sample:s1"           # 候选来自样本库且有当前音色音频


def test_ai_recent_window_excludes_played_candidate(store):
    """防复读：窗口内播过的样本被剔除（关键词直出层），无 fresh 候选时降级现场生成。"""
    pid, _ = _ai_profile(store)
    gen = gen_text()
    sel = pick_first()
    rt = make_rt(store, cfg={"script": {"active_profile": pid}},
                 selector=sel, fallback=gen)

    r1 = asyncio.run(rt.ai_replier({"user": "甲", "content": "怎么下载"}))
    assert r1["text"] == "点手柄下载"                  # 唯一候选 sample:s1（关键词直出）
    r2 = asyncio.run(rt.ai_replier({"user": "乙", "content": "怎么下载"}))
    assert r2 == "现场生成的话术"                       # s1 防复读被剔除，无候选 → 现场生成
    assert gen.calls                                    # 现场生成被调用
    assert len(sel.calls) == 0                          # 关键词层已处理，LLM 未被调用


def test_ai_topic_not_a_reply_candidate(store):
    """话题不再作为回复候选：候选里只有样本，无 topic:id。"""
    pid, _ = _ai_profile(store)
    gen = gen_text()
    sel = pick_first()
    rt = make_rt(store, cfg={"script": {"active_profile": pid}},
                 selector=sel, fallback=gen)
    reply = asyncio.run(rt.ai_replier({"user": "甲", "content": "怎么安装"}))
    assert reply["text"] == "点手柄下载"                 # 只有样本参与
    cands = sel.calls[0]["candidates"]
    assert cands and all(c["kind"] == "sample" for c in cands)  # 无 topic 候选


def test_ai_topic_id_resolves_to_generate(store):
    """LLM 返回 topic:id（旧格式）无法解析为回复 → 降级现场生成。"""
    pid, _ = _ai_profile(store)
    gen = gen_text()
    rt = make_rt(store, cfg={"script": {"active_profile": pid}},
                 selector=pick("topic:t1"), fallback=gen)
    out = asyncio.run(rt.ai_replier({"user": "甲", "content": "讲讲下载"}))
    assert out == "现场生成的话术"   # topic 不再是候选，解析失败 → 现场生成
    assert gen.calls


def test_ai_second_tier_generates_on_miss(store):
    """LLM 不选 → 现场生成文本（永不静默）。"""
    pid, _ = _ai_profile(store)
    gen = gen_text()
    stats = StatsCollector()
    rt = make_rt(store, cfg={"script": {"active_profile": pid}},
                 selector=pick(None), fallback=gen, stats=stats)
    out = asyncio.run(rt.ai_replier({"user": "甲", "content": "主播玩多久了"}))
    assert out == "现场生成的话术"           # str：调用方直接 TTS
    assert gen.calls and gen.calls[0]["content"] == "主播玩多久了"
    assert stats.get("script_ai_generate") == 1


def test_ai_keyword_direct_output(store):
    """关键词直出（第一级）：弹幕命中样本 examples 直接播样本，不走 LLM。"""
    pid, audio_name = _ai_profile(store)
    gen = gen_text()
    sel = pick(None)                          # 关键词命中时 LLM 选择器不应被调用
    stats = StatsCollector()
    rt = make_rt(store, cfg={"script": {"active_profile": pid}},
                 selector=sel, fallback=gen, stats=stats)

    reply = asyncio.run(rt.ai_replier({"user": "甲", "content": "怎么下载"}))
    assert reply["text"] == "点手柄下载"
    assert reply["audio_path"].endswith(audio_name)
    assert sel.calls == []                     # 关键词直出，未走 LLM
    assert gen.calls == []                     # 未降级现场生成
    assert stats.get("script_ai_keyword") == 1


def test_ai_keyword_miss_falls_back_to_llm_select(store):
    """关键词未命中 → LLM 选择兜底（仍可选中样本）。"""
    pid, audio_name = _ai_profile(store)
    gen = gen_text()
    sel = pick("sample:s1")
    rt = make_rt(store, cfg={"script": {"active_profile": pid}},
                 selector=sel, fallback=gen)

    reply = asyncio.run(rt.ai_replier({"user": "甲", "content": "主播玩多久了"}))
    assert reply["text"] == "点手柄下载"       # 关键词没命中，走 LLM 选到 s1
    assert sel.calls                           # LLM 被调用（兜底）
    assert gen.calls == []


def test_ai_keyword_skips_recently_played(store):
    """关键词直出也防复读：刚播过的样本不再直出，走现场生成兜底。"""
    pid, _ = _ai_profile(store)
    gen = gen_text()
    sel = pick(None)
    rt = make_rt(store, cfg={"script": {"active_profile": pid}},
                 selector=sel, fallback=gen)

    r1 = asyncio.run(rt.ai_replier({"user": "甲", "content": "怎么下载"}))
    assert r1["text"] == "点手柄下载"          # 关键词直出命中 s1
    r2 = asyncio.run(rt.ai_replier({"user": "乙", "content": "怎么下载"}))
    assert r2 == "现场生成的话术"              # s1 防复读跳过 → 关键词未命中 → 现场生成


def test_ai_selector_error_still_generates(store):
    """选择器抛异常 → 降级现场生成，不静默。"""
    pid, _ = _ai_profile(store)
    gen = gen_text()
    stats = StatsCollector()
    rt = make_rt(store, cfg={"script": {"active_profile": pid}},
                 selector=boom_selector, fallback=gen, stats=stats)
    out = asyncio.run(rt.ai_replier({"user": "甲", "content": "在吗"}))
    assert out == "现场生成的话术"
    assert stats.get("script_ai_select_error") == 1


def test_ai_sample_without_audio_uses_text(store):
    """无音频样本也参与第一级：命中用其回复文本现场合成（audio_path 空）。"""
    pid, _ = _ai_profile(store, with_audio=False)
    gen = gen_text()
    sel = pick("sample:s1")
    rt = make_rt(store, cfg={"script": {"active_profile": pid}},
                 selector=sel, fallback=gen)
    out = asyncio.run(rt.ai_replier({"user": "甲", "content": "怎么下载"}))
    assert out == {"text": "点手柄下载", "audio_path": ""}  # 文本命中，无音频现场合成
    assert gen.calls == []          # 未降级到现场生成


def test_ai_sample_without_audio_not_in_llm_candidates(store):
    """发声链路统一走当前音色预制音频：无音频样本不进 LLM 候选（无候选 → 现场生成）。"""
    pid, _ = _ai_profile(store, with_audio=False)
    gen = gen_text()
    sel = pick("sample:s1")
    rt = make_rt(store, cfg={"script": {"active_profile": pid}},
                 selector=sel, fallback=gen)
    # 关键词不命中 → 走 LLM 选择层；候选为空（s1 无音频）→ 不打扰 LLM → 现场生成
    out = asyncio.run(rt.ai_replier({"user": "甲", "content": "主播玩多久了"}))
    assert out == "现场生成的话术"
    assert sel.calls == []


def test_ai_stale_audio_falls_back_to_text(store):
    """音频过期（文本已改、text_hash 不符）视为无音频：关键词命中时回退现场 TTS 念新文本。"""
    pid, _ = _ai_profile(store)
    p = store.get_profile(pid)
    p["samples_ai"][0]["reply_text"] = "点右下角小手柄下载"   # 改文本 → 旧音频 stale
    saved = store.save_profile(pid, p)
    assert saved["samples_ai"][0]["stale"] == ["v1"]           # 保存时已标注 stale
    gen = gen_text()
    sel = pick(None)
    rt = make_rt(store, cfg={"script": {"active_profile": pid}},
                 selector=sel, fallback=gen)
    out = asyncio.run(rt.ai_replier({"user": "甲", "content": "怎么下载"}))
    assert out == {"text": "点右下角小手柄下载", "audio_path": ""}


def test_ai_current_voice_switch_changes_audio(store):
    """切当前音色 → 同一条目解析到新音色音频（reload 后热生效）。"""
    pid, v1_audio = _ai_profile(store)
    store.add_voice(pid, "v2", "情绪")
    v2_audio = attach_audio(store, pid, "samples_ai", "s1", "v2", "点手柄下载",
                            name="b.wav")
    rt = make_rt(store, cfg={"script": {"active_profile": pid}},
                 selector=pick(None), fallback=gen_text())
    r1 = asyncio.run(rt.ai_replier({"user": "甲", "content": "怎么下载"}))
    assert r1["audio_path"].endswith(v1_audio)
    store.set_current_voice(pid, "v2")
    rt.reload()
    rt._recent.clear()   # 隔离防复读窗口，聚焦音色切换的解析行为
    r2 = asyncio.run(rt.ai_replier({"user": "乙", "content": "怎么下载"}))
    assert r2["audio_path"].endswith(v2_audio)


def test_ai_no_current_voice_means_no_candidates(store):
    """存档无音色（无当前音色）→ 音频无法解析 → LLM 候选为空 → 现场生成。"""
    pid = store.create_profile("无音色档")["id"]
    p = store.get_profile(pid)
    p["samples_ai"] = [{"id": "s1", "type_name": "问下载", "examples": ["怎么下载"],
                        "reply_text": "点手柄下载", "audios": {}, "enabled": True}]
    store.save_profile(pid, p)
    gen = gen_text()
    sel = pick_first()
    rt = make_rt(store, cfg={"script": {"active_profile": pid}},
                 selector=sel, fallback=gen)
    out = asyncio.run(rt.ai_replier({"user": "甲", "content": "主播玩多久了"}))
    assert out == "现场生成的话术"
    assert sel.calls == []                          # 无候选，LLM 未被打扰
    assert rt.schedule()["items"] == []            # 轮播同样无音频可播


def test_ai_no_fallback_returns_none(store):
    """未注入现场生成器（未配置 LLM）→ None（调用方不挂路由）。"""
    pid, _ = _ai_profile(store)
    rt = make_rt(store, cfg={"script": {"active_profile": pid}},
                 selector=pick("sample:s1"))
    assert asyncio.run(rt.ai_replier({"user": "甲", "content": "在吗"})) is None
