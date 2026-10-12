"""仔仔鼠AI智能体直播助手 · 入口。

当前实现：配置/日志/统计（0.x）、WS 与重放事件源（1.x/2.1）、
事件路由器聚合冷却连击（2.2~2.4）、优先级队列（2.5）、LLM 生成与模板话术
（3.x）、安全过滤与流式 TTS（4.1/4.2）、串行播放调度器（4.3，TTS 密钥就绪
时装配 Player，否则退回打印消费者）、F9 热键（4.4）、口播调度（5.1）、
看门狗（5.2）、启动自检/每 5 分钟周期统计（5.3 终版装配）。
后续：主播侧操作——音频注入联调（4.5，VB-Cable 接直播伴侣）与小号试播（6.1）。
"""
import argparse
import asyncio
import signal
import socket
import sys
import threading
from pathlib import Path

from hub import paths
from hub.config import (ConfigError, agent_settings, agent_status, llm_configured,
                        load_config, mask_secret, script_settings)
from hub.logger import get_logger, setup as setup_logger
from hub.stats import StatsCollector


def _extract_room_num(raw: str) -> str:
    """从房间号或整条直播页 URL 提取纯数字房间号（用户可直接粘贴 URL）。

    https://live.douyin.com/8888?entry=xxx → 8888；非数字原样返回，
    由采集器请求直播页时的 404/解析失败给出可读报错。
    """
    text = str(raw).strip().split("?")[0].rstrip("/")
    if text.isdigit():
        return text
    digits = [seg for seg in text.split("/") if seg.strip().isdigit()]
    return digits[-1].strip() if digits else text


def print_config_summary(cfg: dict) -> None:
    llm = cfg.get("llm", {})
    tts = cfg.get("tts", {})
    audio = cfg.get("audio", {})
    persona = cfg.get("persona", {})
    events = cfg.get("events", {})
    schedule = cfg.get("schedule", {})
    safety = cfg.get("safety", {})
    watchdog = cfg.get("watchdog", {})
    app = cfg.get("app", {})
    webui = cfg.get("webui", {}) or {}
    agent_cfg = agent_settings(cfg)

    lines = [
        "── 配置摘要 ─────────────────────────────",
        f"应用: {app.get('name')}  日志级别: {app.get('log_level')}  重放文件: {app.get('replay_file') or '<无>'}",
        (f"弹幕源: 内置采集器 房间号 {_extract_room_num(cfg.get('danmaku', {}).get('room_num', ''))}"
         if cfg.get("danmaku", {}).get("room_num") else
         f"弹幕监听: {cfg.get('danmaku', {}).get('ws_listen')}（Dycast 外挂）"),
        f"LLM: {llm.get('base_url')}  模型: {llm.get('model')}  密钥: {mask_secret(llm.get('api_key'))}  超时: {llm.get('timeout_seconds')}s",
        f"TTS: 音色 {tts.get('voice_id')}  模型: {tts.get('model')}  密钥: {mask_secret(tts.get('api_key'))}  端点: {tts.get('endpoint')}",
        f"音频: 设备「{audio.get('device')}」  {audio.get('sample_rate')}Hz  声道 {audio.get('channels')}  条间隔 {audio.get('gap_ms')}ms",
        f"人设: {persona.get('nickname')}  上限 {persona.get('max_reply_chars')} 字",
        f"事件: chat合并{events.get('chat', {}).get('merge_window_s')}s"
        f"/过发P1 {events.get('chat', {}).get('stale_drop_p1_s', 90)}s"
        f"·P2 {events.get('chat', {}).get('stale_drop_p2_s', 180)}s"
        f"·其它{events.get('chat', {}).get('stale_drop_s')}s"
        f" 礼物连击{events.get('gift', {}).get('combo_timeout_s')}s"
        f" 关注冷却{events.get('follow', {}).get('cooldown_s')}s/p{events.get('follow', {}).get('probability')}"
        f" 粉丝团p{events.get('fansclub', {}).get('probability')}  进场{'开' if events.get('enter', {}).get('enabled') else '关'}",
        f"口播: {'启用' if schedule.get('enabled') else '停用'}  间隔 {schedule.get('interval_min')} 分钟  话术池 {len(schedule.get('pool', []) or [])} 条",
        f"话术: 存档 {script_settings(cfg)['active_profile'] or '<启动时自动选定>'}（纯AI方案）",
        f"安全: 词库 {safety.get('block_words_file')}  暂停热键 {safety.get('pause_hotkey')}",
        f"看门狗: 失败阈值 {watchdog.get('fail_threshold')} 次",
        ("智能体: " + ("开启（接管对话）" if agent_cfg.get("enabled") else "关闭（规则管道）")
         + f"  检索 {agent_cfg.get('retrieve', {}).get('layer')}"
           f" top{agent_cfg.get('retrieve', {}).get('top_k')}"
         + f"  记忆 {agent_cfg.get('memory', {}).get('max_events')}"
           f"/{agent_cfg.get('memory', {}).get('max_spoken')}"
         + ("  LLM: 已配" if llm_configured(cfg) else "  LLM: 未配（开关置灰，实际以规则管道运行）")),
        f"控制台: {'启用 ' + str(webui.get('listen', '127.0.0.1:8766')) if webui.get('enabled') else '停用（config.yaml webui.enabled 可开启）'}",
        "─────────────────────────────────────────",
    ]
    print("\n".join(lines))


def _make_event_handler(stats: StatsCollector, router, webui=None):
    """事件到达：计数、回显打印（echo 与重放共用）、
    推送控制台事件流、交路由器处理。"""
    from hub.events import LiveEvent

    log_echo = get_logger("ws_echo")

    def on_event(ev: LiveEvent):
        stats.inc("danmaku_in")
        if webui is not None:
            webui.note_event(ev)
        if ev.type == "gift":
            detail = f"gift={ev.gift_name} count={ev.gift_count} value={ev.gift_value} combo_end={ev.combo_end}"
        elif ev.content:
            detail = f"content={ev.content}"
        else:
            detail = ""
        print_safe(f"[事件] type={ev.type} user={ev.user} {detail}")
        log_echo.info("echo_event", {
            "type": ev.type, "user": ev.user, "content": ev.content,
            "gift_name": ev.gift_name, "gift_count": ev.gift_count,
            "gift_value": ev.gift_value, "combo_end": ev.combo_end, "ts": ev.ts,
        })
        router.handle_event(ev)

    return on_event


async def _consume_print(queue, stats: StatsCollector, stop: asyncio.Event) -> None:
    """队列消费侧的临时打印协程（PROMPTS 步骤 2.5：阶段 4 换成 Player）。"""
    log = get_logger("consumer")
    while not stop.is_set():
        try:
            task = await asyncio.wait_for(queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        stats.inc("played")
        extra = f" 合并{task.merged_count}" if task.merged_count > 1 else ""
        tier = f" 档位{task.tier}" if task.tier else ""
        print_safe(f"[任务] P{task.priority} src={task.source}{extra}{tier} 「{task.text}」")
        log.info("task_consumed", {
            "priority": task.priority, "source": task.source, "text": task.text,
            "merged_count": task.merged_count, "tier": task.tier,
            "event_ts": task.event_ts,
        })


async def _await_queue_drained(queue, timeout: float = 300.0) -> None:
    """等待队列彻底排空（每条要么播出要么过发作废）。

    串行播放一条数秒 + 30 秒过发作废兜底，排空必然发生且有限时；
    重放收尾必须等满，否则 played+dropped 与 produced 不自洽（步骤 4.3 验收）。
    """
    async def drained() -> None:
        while queue.qstat()["total"] > 0:
            await asyncio.sleep(0.05)

    try:
        await asyncio.wait_for(drained(), timeout=timeout)
    except asyncio.TimeoutError:
        pass


async def _await_player_idle(player, timeout: float = 30.0) -> None:
    """等最后一条播完（含条间停顿）再置停止（步骤 4.3：防收尾截断声音）。"""
    async def idle() -> None:
        while player.is_playing:
            await asyncio.sleep(0.05)

    try:
        await asyncio.wait_for(idle(), timeout=timeout)
    except asyncio.TimeoutError:
        pass


async def _periodic_stats(stats: StatsCollector, stop: asyncio.Event,
                           interval_s: float = 300.0) -> None:
    """每 5 分钟向控制台输出一次统计摘要（DESIGN §9，与退出 summary 同源）。"""
    log = get_logger("stats")
    try:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                break
            print(stats.format_summary())
            log.info("periodic_stats", stats.summary())
    except asyncio.CancelledError:
        pass


def _ws_port_free(listen: str) -> tuple:
    """线上模式启动自检：监听端口可绑定（未被占用）才能收 Dycast 连接。"""
    host, _, port_s = listen.rpartition(":")
    try:
        port = int(port_s)
    except ValueError:
        return False, f"监听地址格式错误：{listen}（应为 host:port）"
    if not 0 <= port < 65536:
        return False, f"监听端口非法：{listen}"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host or "127.0.0.1", port))
        return True, f"{listen} 可绑定"
    except OSError as e:
        return False, f"{listen} 已被占用（{e}）——可能已有一个中枢在运行，勿重复启动"


def _build_tts(cfg: dict, stats: StatsCollector):
    """流式 TTS 客户端（步骤 4.2）；MiniMax Key 或音色未配置时返回 None，
    播放侧退回打印消费者（与 LLM 占位跳过同一模式，离线重放不白等）。"""
    from hub.tts import TTSError, TTSClient

    try:
        return TTSClient(config=cfg, stats=stats)
    except TTSError as e:
        print(f"[TTS] {e}")
        print("[TTS] 播放侧退回打印模式（不影响路由/队列/话术验证）")
        return None


def _build_templates(cfg: dict):
    """本地模板话术引擎（礼物三档/欢迎/口播，不走 LLM）。"""
    from hub.templates import TemplateEngine

    return TemplateEngine(config=cfg)


def _build_safety(cfg: dict, stats: StatsCollector):
    """敏感词两级过滤（步骤 4.1）；词库加载失败抛 SafetyError 由入口兜底。"""
    from hub.safety import SafetyFilter

    return SafetyFilter.from_config(cfg, stats=stats)


def _migrate_legacy_pool(store, script_rt, cfg: dict) -> None:
    """v3.0 → v3.1 一次性迁移：旧 schedule.pool 文本落成激活存档的 AI 话题条目。

    仅当存档尚无 AI 话题且旧话术池非空时执行；节奏沿用旧 interval_min（换算为秒），
    迁移后旧 schedule.pool 不再参与运行（话术管理页统一维护）。
    """
    profile = script_rt.profile
    if profile is None or profile.get("topics_ai"):
        return
    legacy = (cfg.get("schedule") or {})
    pool = [str(x).strip() for x in (legacy.get("pool") or []) if str(x).strip()]
    if not pool:
        return
    profile["topics_ai"] = [{"id": f"t_{i:02d}", "title": text[:12], "text": text,
                             "audios": {}, "enabled": True}
                            for i, text in enumerate(pool, 1)]
    interval_min = float(legacy.get("interval_min", 7) or 7)
    profile["schedule"] = {"enabled": bool(legacy.get("enabled", True)),
                           "interval_s": max(int(interval_min * 60), 1),
                           "jitter_s": 60}
    store.save_profile(script_rt.profile_id, profile)
    script_rt.reload()
    print(f"[话术] 旧口播话术池 {len(pool)} 条已迁移为存档 AI 话题（话术管理页可见可改）")


def _build_generator(cfg: dict, stats: StatsCollector, safety=None):
    """真实密钥可用时构造 Generator 并打印完整 system 提示词（步骤 3.2 验收）。

    地址/密钥/模型任一未填或仍是 PLACEHOLDER 占位时返回 None：弹幕回复停用
    （弹幕照收照上屏），避免离线重放每条弹幕白等两轮超时。控制台保存
    LLM 配置后由 _reconfigure_llm 重建并热替换。
    """
    from hub.config import llm_configured
    from hub.generator import Generator

    if not llm_configured(cfg):
        print("[LLM] 未配置（地址/密钥/模型任一为空或占位）——弹幕文字回复停用，"
              "礼物/欢迎/口播的本地模板话术不受影响；控制台「配置 LLM」可填写并热生效")
        return None
    sanitizer = safety.sanitize_output if safety is not None else None
    generator = Generator(config=cfg, stats=stats, output_sanitizer=sanitizer)
    print("── 系统提示词（人设模板渲染） ──────────")
    print(generator.system_prompt)
    print("─────────────────────────────────────────")
    return generator


def _reconfigure_stdio() -> None:
    """把标准输出/错误切到 utf-8 + replace：Windows 控制台默认 gbk，弹幕/回复含
    emoji 时 print 会抛 UnicodeEncodeError 并中断事件处理（导致丢弹幕）。"""
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def print_safe(text: str) -> None:
    """控制台打印的编码兜底：reconfigure 在某些运行环境（windowed exe 的 devnull
    流等）不生效，print 仍可能抛 UnicodeEncodeError——打印失败绝不能中断事件处理。"""
    try:
        print(text)
    except Exception:
        pass


async def run_main_loop(cfg: dict, stats: StatsCollector, stop: asyncio.Event = None,
                        echo: bool = False, replay_file: str = None, speed: float = 1.0,
                        reply_wrapper=None, open_browser: bool = False) -> None:
    """主循环：装配 Router→Queue→消费者链路，事件源二选一（重放/WS）。

    Windows 上 asyncio 的 SIGINT 处理经 signal handler 落地：handler 在主线程
    执行并置位 stop，主循环正常返回而非抛 KeyboardInterrupt；后者作为兜底保留。
    stop 可注入，供测试与后续暂停/热键复用。
    reply_wrapper：可选 (builder) -> builder 包装函数，评估脚本（步骤 3.4）
    借此捕获「弹幕原文+生成话术」配对，链路与线上一条代码路径。
    """
    _reconfigure_stdio()
    from hub.queue import PrioritySpeakQueue
    from hub.router import MODE_AGENT, MODE_RULES, Router

    log = get_logger("main")
    room_num = _extract_room_num((cfg.get("danmaku", {}) or {}).get("room_num", "") or "")
    mode = ("replay" if replay_file else
            "builtin" if room_num else
            "echo" if echo else "idle")
    log.info("main_loop_started", {"mode": mode, "room_num": room_num or None})
    if stop is None:
        stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    # 尽早注册 SIGINT handler：装配阶段收到 Ctrl+C 也能优雅退出，且避免装配
    # 耗时导致调用方/测试在装配完成前读不到本 handler（竞态：Python 3.14 的
    # asyncio.run 会先装自己的 _on_sigint，读到并调用它会 cancel 主协程、以
    # KeyboardInterrupt 中断整个 pytest 会话）。
    def _request_stop(*_):
        loop.call_soon_threadsafe(stop.set)

    # 桌面壳（desktop.py）把中枢放在后台线程：signal 只能在主线程注册
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, _request_stop)

    # ── M3 智能体总开关：出厂 OFF，整场行为与旧版一致（V3.1） ──
    # 配置里开了但未配 LLM Key 时降级为规则管道（V3.3）：agent 模式的核心能力是
    # LLM 决策，没有 Key 时开与不开都是「弹幕静默」，不如让 UI 明确显示置灰原因。
    _agent_want = agent_settings(cfg)["enabled"]
    agent_on = bool(_agent_want and llm_configured(cfg))
    if _agent_want and not agent_on:
        print("[智能体] config.yaml 的 agent.enabled 为 true，但未配置 LLM Key——"
              "本次以规则管道启动（控制台配好 LLM 后可一键开启）")
    log.info("agent_switch_at_startup", {"configured": _agent_want, "effective": agent_on})

    queue = PrioritySpeakQueue(config=cfg, stats=stats, no_expire=agent_on)
    safety = _build_safety(cfg, stats)

    # ── v3.2 话术存档运行时：方案指针（ai/template）+ 激活存档 ──
    # 指针留空/失效时兜底创建「默认话术」并存回 config.yaml；旧 schedule.pool
    # 一次性迁移为存档 AI 话题（话术管理页可见可改）
    from hub.config import update_script_config
    from hub.scriptstore import ScriptStore, seed_profiles_if_empty
    from hub.selector import ScriptRuntime

    script_store = ScriptStore()
    # 存档结构 v1 → v3：清空样本/话题库并删除孤儿音频（用户决策：清空重建）
    migrated = script_store.migrate_v1_profiles()
    for m in migrated:
        print(f"[话术] 存档 {m['id']} 已升级到 v3 结构：清空 {m['cleared_topics']} 话题 / "
              f"{m['cleared_samples']} 样本，删除 {m['removed_audio']} 个音频文件")
    # 存档结构 v2 → v3：audio+voice_id → audios 多音色映射，template 分区退役删除
    try:
        from hub.voiceworkshop import VoiceWorkshop

        voice_labels = {str(v.get("voice_id") or ""): str(v.get("voice_name") or "")
                        for v in VoiceWorkshop().voices()}
    except Exception:
        voice_labels = {}
    migrated2 = script_store.migrate_v2_profiles(
        tts_voice_id=str((cfg.get("tts") or {}).get("voice_id") or ""),
        voice_labels=voice_labels)
    for m in migrated2:
        print(f"[话术] 存档 {m['id']} 已升级到 v3 多音色结构：音色 {len(m['voices'])} 个，"
              f"当前直播音色 {m['current_voice'] or '<无>'}，清理模板分区音频 "
              f"{m['removed_audio']} 个")
    # 存档结构 v3 → v4：audios 的扁平 {file, text_hash} 包成 default 情绪档，
    # 补 emotions 档位集合（幂等；迁移前自动备份 profile.yaml 为 profile.yaml.v3.bak）
    migrated3 = script_store.migrate_v3_profiles()
    for m in migrated3:
        print(f"[话术] 存档 {m['id']} 已升级到 v4 情绪档结构："
              f"音色音频 {m['audios']} 份归入 default 档，"
              f"档位集合 {'、'.join(m['emotions'])}（原件备份 {m['backup']}）")
    # 首跑种子（双击版）：profiles 为空时内置示例存档自动落地；激活由下方 reload 兜底选中
    seeded = seed_profiles_if_empty(script_store)
    if seeded:
        print(f"[话术] 首次运行：内置示例存档已落地（{seeded}），已可开箱使用")
    script_rt = ScriptRuntime(script_store, cfg, stats=stats)
    _defaulted_pid = script_rt.reload()
    if _defaulted_pid:
        try:
            update_script_config(str(cfg.get("_config_path") or "config.yaml"),
                                 active_profile=_defaulted_pid)
            cfg.setdefault("script", {})["active_profile"] = _defaulted_pid
        except OSError as e:
            print(f"[话术] 激活存档指针写回 config.yaml 失败：{e}")
            log.warning("script_pointer_write_failed", {"error": str(e)})
    _migrate_legacy_pool(script_store, script_rt, cfg)

    generator = _build_generator(cfg, stats, safety)
    if generator is not None:
        # AI 方案的 system 提示词由存档知识库+AI 样本渲染（无 LLM 时跳过）
        generator.set_system_prompt(script_rt.build_system_prompt(cfg),
                                    script_rt.max_reply_chars())
    reply_builder = generator.generate_reply if generator else None
    if reply_builder is not None and reply_wrapper is not None:
        reply_builder = reply_wrapper(reply_builder)
    _scheme_state = {"llm_replier": reply_builder, "generator": generator}
    # LLM 选择器（template 方案选预制回复 / ai 方案选已有语音）+ AI 未命中时的
    # 现场生成器（None=未配置 LLM，弹幕静默，其余供料照常）
    script_rt.set_llm_selector(generator.select_reply if generator else None)
    script_rt.set_fallback_replier(reply_builder)

    def _current_chat_replier():
        """弹幕应答路由：固定纯AI三级应答（模板方案已退役）；
        未配置 LLM 时返回 None（弹幕照收照上屏但不产出回复任务）。"""
        return script_rt.ai_replier if _scheme_state["llm_replier"] is not None else None

    templates = _build_templates(cfg)
    templates.set_event_templates(script_rt.event_templates())
    router = Router(queue.put, config=cfg, stats=stats,
                    reply_builder=_current_chat_replier(),
                    template_renderer=templates.render,
                    input_filter=safety.check_input,
                    mode=MODE_AGENT if agent_on else MODE_RULES)

    # ── M6 反馈进化：样本库 / 流水落在**活动存档**目录下（换存档自动跟随） ──
    # 每次决策现读（见 `AgentBrain._examples_now`），所以主播点完采纳，下一条就用上。
    from hub.agent.feedback import FeedbackStore

    feedback = FeedbackStore(root=script_store.root,
                             pid_provider=lambda: script_rt.profile_id)

    # ── M4 智能体接管：开关 ON 且有 LLM 时，把决策来源交给 AgentBrain ──
    # 单场记忆随 brain 实例存活，故只在「开关切换 / LLM 配置变更」时重建；
    # 换存档、改话术、调情绪档位都由 brain 经 ScriptRuntime 实时读取，无需重建。
    _brain_state: dict = {"brain": None}

    def _make_brain():
        from hub.agent import AgentBrain

        return AgentBrain(config=cfg, settings=agent_settings(cfg),
                          selector=script_rt, stats=stats, log_dir=paths.LOGS_DIR,
                          examples_provider=feedback.examples)

    def _attach_agent(enabled: bool) -> None:
        """注入 / 撤下接管钩子（幂等）。

        撤下后 `Router._agent_active()` 为假：即便 mode 仍是 agent，弹幕也回落
        M3 的 `reply_builder` 降级路径、礼物/关注回落本地模板——不会因为钩子缺失
        而丢事件或静默。
        """
        if not enabled:
            router.set_agent_handler(None)
            _brain_state["brain"] = None
            return
        brain = _brain_state["brain"] or _make_brain()
        _brain_state["brain"] = brain
        router.set_agent_handler(brain.handle)

    if agent_on:
        _attach_agent(True)
        log.info("agent_attached_at_startup", {"model": _brain_state["brain"].model})

    # 可视化控制台（用户需求）：先建空壳，player/hotkey/事件源装配后注入引用
    from hub.webui import WebUi

    webui = WebUi(config=cfg, stats=stats, queue=queue)

    # 播放侧消费者容器：TTS 未配置时为打印消费者，音色工坊配置 Key 后
    # 热升级为 Player（收尾时展开容器内全部任务）
    consumers: list = []

    # 控制台「配置 LLM」热更新通道：保存即写回 config.yaml 并重建生成器（无需重启）
    # 返回 (ok, message)：webui 层据此向表单就近广播结果（除事件流 note_system 外）
    async def _reconfigure_llm(base_url: str, api_key: str, model: str, test: bool) -> tuple:
        from hub.config import update_llm_config
        from hub.generator import probe_llm

        llm_now = dict(cfg.get("llm", {}) or {})

        def _effective(new: str, current) -> str:
            # 留空 = 保留现值（前端不回显完整密钥）
            return str(new or "").strip() or str(current or "").strip()

        eff_base = _effective(base_url, llm_now.get("base_url"))
        eff_key = _effective(api_key, llm_now.get("api_key"))
        eff_model = _effective(model, llm_now.get("model"))

        def _bad(v: str) -> bool:
            return not v or v.upper().startswith("PLACEHOLDER")

        if _bad(eff_base) or _bad(eff_key) or _bad(eff_model):
            msg = "LLM 配置未保存：接口地址、API 密钥、模型名都需要填真实值"
            return (False, msg)
        if test:
            error = await probe_llm(eff_base, eff_key, eff_model)
            if error:
                msg = f"LLM 测试连接失败，未保存：{error}"
                return (False, msg)
        config_path = str(cfg.get("_config_path") or "config.yaml")
        try:
            update_llm_config(config_path, base_url=eff_base,
                              api_key=eff_key, model=eff_model)
        except OSError as e:
            msg = f"LLM 配置写入 config.yaml 失败：{e}"
            return (False, msg)
        llm_now.update({"base_url": eff_base, "api_key": eff_key, "model": eff_model})
        cfg["llm"] = llm_now
        new_generator = _build_generator(cfg, stats, safety)
        if new_generator is not None:
            new_generator.set_system_prompt(script_rt.build_system_prompt(cfg),
                                            script_rt.max_reply_chars())
        _scheme_state["llm_replier"] = (new_generator.generate_reply
                                        if new_generator else None)
        _scheme_state["generator"] = new_generator
        # LLM 热配置同步替换两方案的选择器与 AI 未命中现场生成器（新配置当场生效）
        script_rt.set_llm_selector(new_generator.select_reply
                                   if new_generator else None)
        script_rt.set_fallback_replier(
            _scheme_state["llm_replier"] if new_generator else None)
        router.set_reply_builder(_current_chat_replier())
        # 智能体若在接管中，同步换掉它的 LLM 客户端（记忆与召回窗口保持不动）
        if _brain_state["brain"] is not None:
            _brain_state["brain"].rebind_llm(cfg)
        msg = f"LLM 配置已保存并热生效（模型 {eff_model}，已写入 config.yaml）"
        return (True, msg)

    webui.llm_configurer = _reconfigure_llm

    # 控制台「系统配置 → 兜底口播」热更新通道：改文案 / 开关注销无需重启
    async def _reconfigure_fallback(text: str, enabled: bool) -> tuple:
        from hub.config import update_safety_config

        text = str(text or "").strip()
        if enabled and not text:
            return (False, "兜底口播已开启但文案为空：请填写文案或关闭兜底口播")

        safety_now = dict(cfg.get("safety", {}) or {})
        config_path = str(cfg.get("_config_path") or "config.yaml")
        try:
            update_safety_config(config_path, fallback_reply=text,
                                 fallback_enabled=enabled)
        except OSError as e:
            return (False, f"兜底配置写入 config.yaml 失败：{e}")

        safety_now["fallback_reply"] = text
        safety_now["fallback_enabled"] = enabled
        cfg["safety"] = safety_now
        # 热生效：现场生成器 + 敏感词过滤器（router 读 cfg 即时生效，无需重建）
        safety.set_fallback(text, enabled)
        gen = _scheme_state.get("generator")
        if gen is not None:
            gen.set_fallback(text, enabled)
        return (True, "兜底口播已保存并热生效")

    webui.fallback_configurer = _reconfigure_fallback

    # 控制台「音色工坊」热更新通道：保存 MiniMax Key（无需重启）。
    # 直播音色不再经此通道——由话术管理页的存档 current_voice 驱动（_apply_live_voice），
    # config.yaml 的 tts.voice_id 仅作启动兜底，页面不再写它。
    # 返回 (ok, message)：webui 层据此广播 config_done（前端按钮复位+就近提示）
    async def _reconfigure_tts(api_key: str, endpoint: str, test: bool) -> tuple:
        from hub.tts import TTSError, TTSClient
        from hub.player import Player, PlayerError

        workshop = webui.voice_workshop
        tts_now = dict(cfg.get("tts", {}) or {})
        # 留空 = 保留现值（前端不回显完整密钥；端点同理）
        eff_key = str(api_key or "").strip() or str(tts_now.get("api_key", "") or "").strip()
        eff_ep = str(endpoint or "").strip() or str(tts_now.get("endpoint", "") or "").strip()

        if not eff_key:
            msg = "MiniMax 配置未保存：请先填写 API Key"
            return (False, msg)
        if test:
            # 连通性验证：用「即将保存的」Key（留空时 workshop 内部回退现有 Key）
            error = await asyncio.to_thread(
                workshop.probe_key, api_key.strip() or None, endpoint.strip() or None)
            if error:
                msg = f"MiniMax 测试连接失败，未保存：{error}"
                return (False, msg)
        # 写盘：Key/端点进 config.json（音色工坊数据目录）
        try:
            if api_key.strip() or endpoint.strip():
                await asyncio.to_thread(workshop.save_key, eff_key, eff_ep)
        except Exception as e:
            msg = f"MiniMax 配置写入失败：{e}"
            return (False, msg)
        tts_now.update({"api_key": eff_key, "endpoint": eff_ep})
        cfg["tts"] = tts_now
        # 发声音色以存档当前音色为准（含微调参数）：配好 Key 当场即可升级出声
        _apply_live_voice()

        # 重建 TTS 客户端并热生效：已有 Player 换引用；打印模式升级为 Player
        nonlocal player
        try:
            new_tts = TTSClient(config=cfg, stats=stats)
        except TTSError as e:
            msg = f"MiniMax 配置已保存，但 TTS 未启用：{e}"
            return (False, msg)
        if player is not None:
            player.swap_tts(new_tts)
            msg = f"TTS 配置已保存并热生效（音色 {cfg.get('tts', {}).get('voice_id')}）"
            return (True, msg)
        try:
            new_player = Player(queue, new_tts, stats=stats, config=cfg)
            new_player.check_device()
        except PlayerError as e:
            msg = f"MiniMax 配置已保存，但播放启动失败：{e}"
            return (False, msg)
        new_player.on_play_start = webui.note_play_start
        new_player.on_play_end = webui.note_play_end
        new_player.on_device_fallback = _note_device_fallback
        if consumers:
            consumers[0].cancel()
            consumers.clear()
        consumers.append(asyncio.create_task(new_player.run(stop)))
        player = new_player
        webui.player = new_player
        if hotkey is not None:
            hotkey.player = new_player
        msg = f"TTS 已就绪并开始出声（音色 {cfg.get('tts', {}).get('voice_id')}，无需重启）"
        return (True, msg)

    webui.tts_configurer = _reconfigure_tts

    def _change_audio_device(device: str) -> tuple:
        """控制台切换输出声卡：写回 config.yaml + Player 热切换。"""
        from hub.config import update_audio_device

        device = str(device or "").strip()
        if not device:
            return (False, "未选择输出设备")
        audio = cfg.setdefault("audio", {})
        old = str(audio.get("device", "") or "")
        audio["device"] = device
        # 写盘（行级替换保留注释）
        try:
            update_audio_device(str(cfg.get("_config_path") or "config.yaml"), device)
        except Exception as e:
            audio["device"] = old  # 回滚内存态
            return (False, f"写入 config.yaml 失败：{e}")
        # Player 热切换
        if player is not None:
            player.swap_device(device)
        label = "虚拟声卡" if "CABLE" in device.upper() else "本机扬声器"
        return (True, f"输出已切换到 {label}「{device}」（下一条话术生效）")

    webui.audio_device_changer = _change_audio_device

    def _note_device_fallback(name: str) -> None:
        """播放时找不到配置设备（蓝牙耳机关机/被拔等）→ 事件流可见提示，防「切换没生效」错觉。"""
        webui.note_system(f"找不到输出设备「{name}」，本条已回退到系统默认输出设备")
        webui.toast(f"输出设备「{name}」不存在，已用系统默认输出", "error")

    async def _stream_text_to_device(text: str, device: str,
                                     tts_overrides: dict = None) -> tuple:
        """把一段文本合成并播放到指定设备；tts_overrides 可覆盖 voice_id/speed/vol/pitch/emotion。

        声卡试听与音色工坊「参数微调试听」共用；无覆盖时优先复用 Player 的 TTS 客户端。
        """
        from hub.config import minimax_configured

        if not minimax_configured(cfg):
            return (False, "请先在音色工坊配置 MiniMax Key")
        tts_client = None
        if not tts_overrides:
            tts_client = getattr(player, "_tts", None) if player is not None else None
        if tts_client is None:
            try:
                from hub.tts import TTSClient
                tts_cfg = dict(cfg)
                if tts_overrides:
                    tts_sec = dict(cfg.get("tts", {}) or {})
                    tts_sec.update({k: v for k, v in tts_overrides.items()
                                    if v is not None})
                    tts_cfg["tts"] = tts_sec
                tts_client = TTSClient(config=tts_cfg, stats=stats)
            except Exception as e:
                return (False, f"TTS 初始化失败：{e}")
        audio_cfg = cfg.get("audio", {}) or {}
        sample_rate = int(audio_cfg.get("sample_rate", 24000))
        channels = int(audio_cfg.get("channels", 1))
        try:
            import sounddevice as sd
            from hub.webui import resolve_output_device
            device_idx = resolve_output_device(device)
            stream = sd.RawOutputStream(
                samplerate=sample_rate, channels=channels,
                dtype="int16", device=device_idx)
            stream.start()
        except Exception as e:
            return (False, f"打开设备失败：{e}")

        def _close(s):
            # in-flight write 撞 stop/close 属常态（同 Player._close_stream 口径），静默
            for fn in (s.stop, s.close):
                try:
                    fn()
                except Exception:
                    pass

        try:
            async for chunk in tts_client.synthesize_stream(text):
                await asyncio.to_thread(stream.write, chunk)  # 阻塞写卸载线程，防冻结事件循环
            try:
                stream.stop()
            except Exception:
                pass
            await asyncio.sleep(0.05)
            try:
                stream.close()
            except Exception:
                pass
            return (True, f"试听已播放到「{device}」")
        except Exception as e:
            _close(stream)
            return (False, f"合成或播放失败：{type(e).__name__}: {e}")

    async def _test_audio_device(device: str) -> tuple:
        """TTS 试听：合成「音频测试」播放到指定设备（不影响 Player 当前设备）。"""
        return await _stream_text_to_device("音频测试", device)

    async def _voice_param_preview(voice_id: str, text: str, params: dict) -> tuple:
        """音色工坊「参数微调」试听：跟随系统配置的输出设备（用户需求，不走浏览器）。"""
        device = str((cfg.get("audio", {}) or {}).get("device", "") or "")
        if not device:
            return (False, "未配置输出设备（系统配置页选择输出声卡）")
        overrides = {"voice_id": voice_id}
        overrides.update(params or {})
        return await _stream_text_to_device(
            text or "家人们晚上好，欢迎来到直播间！", device, overrides)

    webui.audio_device_tester = _test_audio_device
    webui.voice_param_previewer = _voice_param_preview

    from hub.watchdog import Watchdog, default_notify

    def _watchdog_notify(title: str, message: str) -> None:
        default_notify(title, message)          # 控制台 + 桌面通知（原行为保留）
        webui.note_system(f"看门狗 · {title}：{message}")

    watchdog = Watchdog(queue, config=cfg, stats=stats, notify=_watchdog_notify)
    on_event = _make_event_handler(stats, router, webui)

    tts = _build_tts(cfg, stats)
    player = None
    if tts is not None:
        from hub.player import Player

        player = Player(queue, tts, stats=stats, config=cfg)
        player.on_play_start = webui.note_play_start
        player.on_play_end = webui.note_play_end
        player.on_device_fallback = _note_device_fallback
        webui.player = player
        consumers.append(asyncio.create_task(player.run(stop)))
    else:
        consumers.append(asyncio.create_task(_consume_print(queue, stats, stop)))

    # ── 启动自检（PROMPTS 步骤 5.3）：逐项检查并打印，任一失败即退出并指明原因 ──
    problems: list[str] = []
    from hub.config import minimax_configured

    rows = [("配置完整性", True,
             "LLM 密钥就绪" if generator is not None else
             "LLM 未配置（不影响启动，弹幕回复停用，控制台「配置 LLM」可填）")]
    rows.append(("MiniMax TTS", True,
                 "密钥就绪" if minimax_configured(cfg) else
                 "未配置（不影响启动，不出声，控制台「音色工坊」可配置）"))
    rows.append(("敏感词库", True, f"{safety.word_count} 条词条已加载"))
    if replay_file:
        replay_ok = Path(replay_file).exists()
        rows.append(("重放文件", replay_ok, str(replay_file)))
        if not replay_ok:
            problems.append(f"重放文件不存在: {replay_file}")
    if player is not None:
        from hub.player import PlayerError

        try:
            player.check_device()  # 设备名拼错即启动失败并给出可用清单（步骤 4.3 容错）
            device = cfg.get("audio", {}).get("device") or "系统默认输出"
            rows.append(("输出声卡设备", True, f"「{device}」可用"))
        except PlayerError as e:
            rows.append(("输出声卡设备", False, str(e)))
            problems.append(str(e))
    else:
        rows.append(("输出声卡设备", True, "TTS 密钥未配置，播放侧打印模式（跳过声卡检查）"))
    listen = None
    if not replay_file:
        if room_num:
            # 内置采集器模式：不监听 8765，无端口可检；房间号在装配时校验。
            # 真连状态（含 DEVICE_BLOCKED 风控拒绝）属运行期状态，交控制台 source 面板展示
            rows.append(("弹幕源·内置采集器", bool(room_num), f"房间号 {room_num}（连接状态见控制台）"))
        else:
            listen = str(cfg.get("danmaku", {}).get("ws_listen", "127.0.0.1:8765"))
            port_free, detail = _ws_port_free(listen)
            rows.append(("WS 监听端口", port_free, detail))
            if not port_free:
                problems.append(detail)
    if webui.enabled:
        ui_listen = str((cfg.get("webui", {}) or {}).get("listen", "127.0.0.1:8766"))
        ui_free, ui_detail = _ws_port_free(ui_listen)
        rows.append(("控制台端口", ui_free, f"{ui_detail}（页面 http://{ui_listen}/）"))
        if not ui_free:
            problems.append(ui_detail)
    print("── 启动自检 ─────────────────────────────")
    for name, ok, detail in rows:
        print(f" [{'通过' if ok else '失败'}] {name}: {detail}")
    print("─────────────────────────────────────────")
    if problems:
        raise RuntimeError("启动自检未通过：\n" + "\n".join(problems))

    router_task = asyncio.create_task(router.run())

    from hub.scheduler import Scheduler

    scheduler = Scheduler(queue, config=cfg, stats=stats)

    def _apply_live_voice() -> None:
        """存档当前直播音色 → 内存 cfg.tts（voice_id + 微调参数）→ 重建 TTSClient 热替换。

        发声音色以存档为准（config.yaml 的 tts.voice_id 仅启动兜底，页面不再写它）：
        参数优先级 = 存档 voice.params > 音色工坊档案参数 > 默认值。
        MiniMax 未配置（TTSClient 起不来）时只更新内存 cfg，后续 _reconfigure_tts
        配好 Key 时自然带上。切存档/切音色/改参数经 script_applier 链即时热生效。
        """
        from hub.scriptstore import DEFAULT_VOICE_PARAMS
        from hub.tts import TTSError, TTSClient

        voice = script_rt.current_voice()
        if not voice:
            return
        merged = dict(DEFAULT_VOICE_PARAMS)
        try:
            ws_params = webui.voice_workshop.voice_params(voice) or {}
        except Exception:
            ws_params = {}
        merged.update({k: v for k, v in ws_params.items() if k in merged})
        merged.update({k: v for k, v in script_rt.voice_params(voice).items()
                       if k in merged})
        tts_now = cfg.setdefault("tts", {})
        target = {"voice_id": voice, **merged}
        if all(tts_now.get(k) == v for k, v in target.items()):
            return
        tts_now.update(target)
        log.info("live_voice_applied", {"voice_id": voice, **merged})
        try:
            new_tts = TTSClient(config=cfg, stats=stats)
        except TTSError:
            return  # MiniMax 未配置：仅更新内存 cfg，配好 Key 时再升级
        if player is not None:
            player.swap_tts(new_tts)

    def _apply_script_runtime() -> None:
        """话术存档热生效：提示词、事件模板、轮播供料、弹幕应答路由、直播音色一次刷新。

        控制台保存存档内容 / 切换激活存档 / 切换存档音色后调用（无需重启）。
        """
        gen = _scheme_state.get("generator")
        if gen is not None:
            gen.set_system_prompt(script_rt.build_system_prompt(cfg),
                                  script_rt.max_reply_chars())
        templates.set_event_templates(script_rt.event_templates())
        router.set_event_enabled(script_rt.event_enabled())
        if script_rt.profile is not None:
            sc = script_rt.schedule()
            scheduler.configure(enabled=sc["enabled"], interval_s=sc["interval_s"],
                                jitter_s=sc["jitter_s"], items=sc["items"])
        router.set_reply_builder(_current_chat_replier())
        _apply_live_voice()
        log.info("script_runtime_applied", script_rt.status())

    _apply_script_runtime()  # 启动即按激活存档供料（无存档时各组件保持旧配置行为）
    webui.script_store = script_store
    webui.script_runtime = script_rt
    webui.script_applier = _apply_script_runtime
    webui.feedback_store = feedback          # M6：反馈采集 / 统计（页面动作族用）
    webui.live_controller = scheduler.set_live
    webui.live_getter = lambda: scheduler.is_live

    # ── M6 对话调参：说人话改配置（写前快照 + 可撤销 + 热生效） ──
    # 与决策共用同一套 llm 配置、不额外要 Key；未配 Key 时 SessionTuner.available 为假，
    # 页面把输入区置灰并说明原因（V6.7），话术管理表单不受影响。
    from hub.agent.llm import AgentLLM
    from hub.agent.session import SNAPSHOT_FILE, SessionTuner

    def _apply_agent_tuning(changes) -> list:
        """调参热生效：先让运行时重读存档（改的是 profile.yaml），再走存档应用链
        —— 提示词、事件模板、轮播供料（含新的 interval_s）一次刷新。
        `Scheduler.configure` 会打断进行中的长睡，所以间隔改动是「立刻」生效而不是
        等旧周期睡完（否则最长要等 60 分钟，表现像"没生效"）。
        """
        script_rt.reload()
        _apply_script_runtime()
        return ["存档与调度器已按新值生效"]

    webui.agent_session = SessionTuner(
        llm=AgentLLM(config=cfg, stats=stats),      # 构造不发请求，未配 Key 时 available 为假
        store=script_store,
        pid_provider=lambda: script_rt.profile_id,
        config_path=str(cfg.get("_config_path") or "config.yaml"),
        applier=_apply_agent_tuning,
        cfg=cfg,
        snapshot_path=paths.LOGS_DIR / SNAPSHOT_FILE,
    )

    # ── M3 智能体总开关：控制台「智能体」卡的状态与热切换 ──

    def _agent_runtime_status() -> dict:
        """智能体状态快照（含运行时真实模式）。

        与 config.agent_status 的差别：这里的 enabled / mode / no_expire 反映**运行时**
        状态（V3.3 配置开了但未配 LLM 时实际跑的是规则管道），并附 configured_enabled /
        downgraded / note 供前端解释「为什么开关打不开」。
        """
        st = agent_status(cfg)
        st["mode"] = router.mode
        # 运行时是否真在接管 = 模式 + 钩子（M4 起二者必须同时成立，见 Router._agent_active）
        st["enabled"] = bool(router.mode == MODE_AGENT and router.agent_attached)
        st["no_expire"] = queue.no_expire
        st["configured_enabled"] = agent_settings(cfg)["enabled"]
        st["downgraded"] = bool(st["configured_enabled"] and not st["available"])
        if st["downgraded"]:
            st["note"] = "配置里已开启，但未配置 LLM Key——本次以规则管道运行"
        # M5「智能体」页状态条要用：模型名与本场记忆用量（无 brain 时为空）
        brain = _brain_state["brain"]
        st["model"] = brain.model if brain is not None else ""
        st["session"] = brain.memory.stats() if brain is not None else {}
        return st

    async def _set_agent_enabled(enabled: bool) -> tuple:
        """总开关热切换（V3.2）：写回 config.yaml + 切 Router/Queue 模式，无需重启。

        先落盘再切运行时：写盘失败就整个不动（避免「这次生效、重启又回去」的假象）。
        返回 (ok, message) 供控制台回显。
        """
        from hub.config import update_agent_config

        want = bool(enabled)
        if want and not llm_configured(cfg):
            return (False, "未配置 LLM Key，无法开启智能体（请先在「LLM」卡配置密钥）")
        config_path = str(cfg.get("_config_path") or "config.yaml")
        try:
            update_agent_config(config_path, {"enabled": want})
        except OSError as e:
            return (False, f"智能体开关写入 config.yaml 失败：{e}")
        cfg.setdefault("agent", {})["enabled"] = want
        mode = router.set_mode(MODE_AGENT if want else MODE_RULES)
        queue.set_no_expire(want)
        # 先切运行时状态、再挂/摘接管钩子：`_agent_active()` 两个条件同时成立才算接管
        _attach_agent(want)
        log.info("agent_switch_applied", {"enabled": want, "mode": mode,
                                         "no_expire": queue.no_expire,
                                         "attached": router.agent_attached})
        if want:
            return (True, "智能体已开启并接管对话：弹幕/礼物/关注逐条决策，队列不再丢过期任务（无需重启）")
        return (True, "智能体已关闭：恢复规则管道（聚合窗口/连击/冷却与过期丢弃照旧）")

    webui.agent_status_provider = _agent_runtime_status
    webui.agent_switcher = _set_agent_enabled

    # ── M5 智能体页面：决策透视数据源 + 试玩调试执行器 ──
    def _decision_logs(limit: int) -> dict:
        """决策透视（DESIGN §7.2④）：最近 limit 条决策 + 四张聚合卡。

        倒序在服务端做（`reversed`）：页面拿到的就是"最新在上"的列表，不用自己再排。
        聚合卡按**原始时间顺序**统计（`summarize` 对顺序不敏感，但先把语义说清楚）。
        brain 未装配（总开关 OFF）时回空 + 说明文案，页面不必自己判断。
        """
        from hub.agent.log import summarize as summarize_decisions

        brain = _brain_state["brain"]
        if brain is None:
            return {"available": False, "records": [], "summary": {}, "path": "",
                    "note": "智能体未接管：开启「系统配置 → 智能体」总开关后，这里会显示逐步决策"}
        logger = brain.decision_log
        try:
            records = logger.read_recent(limit)
        except Exception as e:      # 读日志失败不该让页面空白无解释
            log.warning("agent_logs_read_failed", {"error": f"{type(e).__name__}: {e}"})
            return {"available": True, "records": [], "summary": {}, "path": "",
                    "note": f"读取决策日志失败：{e}"}
        return {"available": True, "records": list(reversed(records)),
                "summary": summarize_decisions(records),
                "path": str(logger.path() or ""), "note": ""}

    async def _run_playground(payload: dict) -> dict:
        """试玩调试（DESIGN §7.2②）：走 `AgentBrain.preview`，零影响直播链路（V5.3）。"""
        from hub.agent.playground import Playground, build_batch
        import time as _time

        brain = _brain_state["brain"]
        events = payload.get("events")
        if not events:
            batch = payload.get("batch") or {}
            events = build_batch(
                str(batch.get("type") or ""), batch.get("count") or 1,
                user=str(batch.get("user") or ""),
                content=str(batch.get("content") or ""),
                gift_name=str(batch.get("gift_name") or ""),
                gift_count=batch.get("gift_count"),
                gift_value=batch.get("gift_value"))
        playground = Playground(brain, clock=_time.time, log=log)
        result = await playground.run(events)
        # 试玩一律不落决策日志：否则"本场决策数"会把预演算进去（见 playground 模块头）
        log.info("agent_playground_ran", {"count": result.get("summary", {}).get("count", 0),
                                          "attached": router.agent_attached})
        return result

    webui.agent_logs_provider = _decision_logs
    webui.agent_playground = _run_playground

    # 话题让位探针：有弹幕回复（排队/生成中/合并窗口）时，话题轮播暂停推进、等回复结束
    def _reply_busy() -> bool:
        q = queue.qstat()
        return bool(q["p0"] or q["p1"] or router.has_pending_reply())
    scheduler.set_reply_busy(_reply_busy)

    scheduler_task = asyncio.create_task(scheduler.run(stop))
    watchdog_task = asyncio.create_task(watchdog.run(stop))
    stats_task = asyncio.create_task(_periodic_stats(stats, stop))

    from hub.hotkey import HotkeyController

    def _hotkey_notify(msg: str) -> None:
        print(msg)
        webui.note_system(msg)

    hotkey = HotkeyController(router, queue, player, config=cfg, stats=stats,
                              notify=_hotkey_notify)
    webui.hotkey = hotkey
    print(f"智能体: {'已开启（接管对话，队列不丢过期任务）' if router.mode == MODE_AGENT else '已关闭（规则管道，行为与旧版一致）'}"
          f"　控制台「系统配置 → 智能体」可随时切换（无需重启）")
    if hotkey.start():
        print(f"热键：按 {hotkey.hotkey_name.upper()} 暂停/恢复 AI 发声（人工保底）")
    else:
        print(f"[警告] 全局热键 {hotkey.hotkey_name.upper()} 启动失败，"
              f"人工保底退回 Ctrl+C 完全停止")

    ws_task = None
    ws = None
    collector = None
    collector_task = None
    danmaku_ctl: dict = {"collector": None, "task": None, "room": None}  # set_room 热切换 / cookie 重连用
    connect_lock = asyncio.Lock()  # 连接/换房全程串行：防双击或 cookie 重连并发起两个采集器

    if replay_file:
        from hub.replay import ReplaySource

        source = ReplaySource(replay_file, on_event, speed=speed)
        print(f"重放模式：{replay_file}（speed={speed}）")

        async def replay_and_stop():
            await source.run()
            # 重放收尾：先停口播调度、看门狗与周期统计（防收尾窗口再入队/再打印破坏自洽），再排空
            # 路由器在途产出（聚合窗口/连击/冷却积压），等队列消费完毕、
            # 最后一条播完后再置停止——保证统计自洽且不截断声音（PROMPTS 步骤 2.5/4.3 验收）
            scheduler_task.cancel()
            watchdog_task.cancel()
            stats_task.cancel()
            router.finalize()
            await router.drain()
            await _await_queue_drained(queue)
            if player is not None:
                await _await_player_idle(player)
            await asyncio.sleep(0.1)
            stop.set()

        replay_task = asyncio.create_task(replay_and_stop())
    else:
        # 内置采集器（步骤2 接线）：DyMessage dict 经 normalize 转 LiveEvent，
        # 与 ws_server/replay 事件源同一条下游链路；控制台 set_room 可热切换房间
        import time as _time

        from hub.douyin import DouyinCollector, DouyinError
        from hub.events import normalize

        def _dy_on_event(dy: dict) -> None:
            ev = normalize(dy, ts=_time.time())
            if ev is not None:
                on_event(ev)

        def _current_cookie() -> str:
            """每次连接现读 cfg（Cookie 卡保存后重连即生效，无需重启）。"""
            return str((cfg.get("danmaku", {}) or {}).get("cookie", "") or "")

        def _make_state_toast(collector):
            """采集器状态变化 → 顶部胶囊通知（连接成功/失败/重连）。"""
            def _on_state(state: str) -> None:
                if state == "live":
                    webui.toast(f"已连接房间 {collector.room_num}", "success")
                elif state == "wait_living":
                    webui.toast(f"连接失败：{collector.last_error}", "error")
                elif state == "reconnecting":
                    webui.toast("连接中断，正在重连…", "info")
            return _on_state

        replay_task = None

        async def _connect_room(room_raw: str) -> None:
            """控制台贴房间号 → 停旧采集器（及 8765 外挂监听）→ 连新房间。

            全程持锁：函数体里多处 await（停旧任务/关 8765），并发进入会在挂起
            窗口期读到空 danmaku_ctl 而各起一个采集器——双连接互踢并被抖音限流。
            """
            async with connect_lock:
                await _connect_room_locked(room_raw)

        async def _connect_room_locked(room_raw: str) -> None:
            new_room = _extract_room_num(room_raw)
            if not new_room or not any(ch.isdigit() for ch in new_room):
                webui.note_system("连接房间失败：请粘贴抖音直播间房间号或直播页链接")
                return
            old_task = danmaku_ctl["task"]
            if old_task is not None:
                old_task.cancel()
                try:
                    await old_task
                except (asyncio.CancelledError, Exception):
                    pass
            if ws is not None:  # 一次一个弹幕源：贴房间号后停掉外挂监听
                await ws.close()
                if ws_task is not None:
                    ws_task.cancel()
            cookie_now = _current_cookie()
            try:
                new_collector = DouyinCollector(new_room, _dy_on_event,
                                                 cookie=cookie_now)
            except DouyinError as e:
                webui.note_system(f"连接房间失败：{e}")
                webui.toast(f"连接房间失败：{e}", "error")
                return
            new_collector.on_state_change = _make_state_toast(new_collector)
            danmaku_ctl["collector"] = new_collector
            danmaku_ctl["room"] = new_room
            danmaku_ctl["task"] = asyncio.create_task(new_collector.run(stop))
            webui.source_state = new_collector.status
            webui.note_system(
                f"内置采集器正在连接房间 {new_room}"
                f"（原生签名直连，{'携带 cookie 增强' if cookie_now.strip() else '无 cookie'}）")

        webui.room_connector = lambda room: asyncio.create_task(_connect_room(room))

        async def _cookie_configurer(cookie: str) -> tuple:
            """系统配置 Cookie 卡：写回 config.yaml + 更新内存；已连接房间自动重连生效。"""
            from hub.config import update_cookie_config
            value = str(cookie or "").strip()
            try:
                update_cookie_config(str(cfg.get("_config_path") or "config.yaml"), value)
            except OSError as e:
                return (False, f"Cookie 写回 config.yaml 失败：{e}")
            cfg.setdefault("danmaku", {})["cookie"] = value
            if not value:
                return (True, "Cookie 已清空（观看限制直播间将无法抓取事件）")
            room_now = danmaku_ctl.get("room")
            if room_now:
                await _connect_room(room_now)  # 携带新 cookie 重连当前房间
                return (True, f"Cookie 已保存（{len(value)} 字符），已携带新 Cookie 重连房间 {room_now}")
            return (True, f"Cookie 已保存（{len(value)} 字符），连接房间时生效")

        webui.cookie_configurer = _cookie_configurer

        async def _disconnect_room() -> None:
            """控制台「断开」：停掉内置采集器，回到待连接状态（可再贴房间号重连）。

            与 _connect_room 共用同一把锁：断开与连接并发时谁先谁后都有确定结果，
            不会出现「断开到一半新采集器已启动、随后被误杀/或旧采集器漏杀」。
            """
            async with connect_lock:
                old_task = danmaku_ctl["task"]
                if old_task is None:
                    webui.note_system("当前没有已连接的房间")
                    return
                old_task.cancel()
                try:
                    await old_task
                except (asyncio.CancelledError, Exception):
                    pass
                old_collector = danmaku_ctl["collector"]
                if old_collector is not None:
                    old_collector.state = "closed"
                danmaku_ctl["collector"] = None
                danmaku_ctl["task"] = None
                danmaku_ctl["room"] = None
                webui.source_state = lambda: {"mode": "idle"}
                webui.note_system("已断开与房间的弹幕连接（可随时粘贴房间号重新连接）")
                webui.toast("已断开房间", "success")

        webui.room_disconnector = lambda: asyncio.create_task(_disconnect_room())

        if room_num:
            dy_cookie = _current_cookie()
            try:
                collector = DouyinCollector(room_num, _dy_on_event, cookie=dy_cookie)
                collector.on_state_change = _make_state_toast(collector)
            except DouyinError as e:
                raise RuntimeError(f"启动自检未通过：\n{e}") from e
            danmaku_ctl["collector"] = collector
            danmaku_ctl["room"] = room_num
            danmaku_ctl["task"] = asyncio.create_task(collector.run(stop))
            collector_task = danmaku_ctl["task"]
            print(f"线上模式：内置采集器连接房间 {room_num}"
                  f"（原生签名直连，{'携带 cookie 增强' if dy_cookie.strip() else '无 cookie'}，Ctrl+C 退出）")
        else:
            from hub.ws_server import WsServer

            ws = WsServer(on_event, listen)
            await ws.start()
            ws_task = asyncio.create_task(ws.serve_forever())
            print(f"线上模式：等待 Dycast 连接 {listen}（Ctrl+C 退出；"
                  f"控制台贴房间号可切换为内置采集器）")

    # ── 可视化控制台装配与启动（用户需求：网页查看/操控中枢） ──
    def _send_test_event(kind: str, payload: dict = None) -> None:
        """控制台测试：注入 DyMessage，走与真实事件完全相同的链路（test 标记，
        router 据此绕过冷却/概率/enter 开关，稳定产出回复）。"""
        import time as _time

        from hub.events import normalize

        payload = payload or {}
        user = str(payload.get("user") or "").strip() or "老铁"
        if kind == "gift":
            dy = {"method": "WebcastGiftMessage", "user": {"name": user},
                  "gift": {"name": str(payload.get("gift_name") or "小心意"),
                           "count": int(payload.get("gift_count") or 1),
                           "price": int(payload.get("gift_value") or 10),
                           "repeatEnd": True},
                  "test": True}
        elif kind == "follow":
            dy = {"method": "WebcastSocialMessage", "user": {"name": user}, "test": True}
        elif kind == "fansclub":
            dy = {"method": "WebcastFansclubMessage", "user": {"name": user},
                  "content": str(payload.get("content") or ""), "test": True}
        elif kind == "enter":
            dy = {"method": "WebcastMemberMessage", "user": {"name": user}, "test": True}
        else:  # chat
            dy = {"method": "WebcastChatMessage", "user": {"name": user},
                  "content": str(payload.get("content") or ""), "test": True}
        ev = normalize(dy, ts=_time.time())
        if ev is not None:
            on_event(ev)

    webui.test_event_sender = _send_test_event
    if replay_file:
        webui.source_state = lambda: {"mode": "replay"}
    elif collector is not None:
        webui.source_state = collector.status
    elif ws is not None:
        webui.source_state = lambda: {"mode": "live", "connected": ws.clients_connected,
                                      "listen": listen}

    webui_task = None
    if webui.enabled:
        await webui.start()
        webui_task = asyncio.create_task(webui.serve_forever())
        print(f"可视化控制台: {webui.url}")
        if open_browser and not replay_file:
            import webbrowser

            try:
                webbrowser.open(webui.url)
            except Exception:
                pass

    await stop.wait()

    hotkey.stop()
    router.stop()
    if ws is not None:
        await ws.close()
    if webui_task is not None:
        webui_task.cancel()
    tasks = (ws_task, replay_task, collector_task, router_task, scheduler_task,
             watchdog_task, stats_task, *consumers, webui_task)
    for task in tasks:
        if task is not None:
            task.cancel()
    for task in tasks:
        if task is not None:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    await webui.close()
    log.info("main_loop_stopped")


def main() -> int:
    parser = argparse.ArgumentParser(description="仔仔鼠AI智能体直播助手")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    parser.add_argument("--check-config", action="store_true", help="只做配置加载与校验，不启动")
    parser.add_argument("--replay", metavar="FILE", help="重放模式指定 JSONL 文件（步骤 2.1 起生效）")
    parser.add_argument("--speed", type=float, default=1.0, metavar="N", help="重放倍速（默认 1.0，与 --replay 搭配）")
    parser.add_argument("--echo", action="store_true", help="回显模式：启动 WS 服务端，打印收到的白名单消息摘要")
    args = parser.parse_args()

    if args.check_config:
        try:
            cfg = load_config(args.config)
        except ConfigError as e:
            print("配置校验未通过，缺失/问题清单：")
            print(str(e))
            return 1
        print_config_summary(cfg)
        print("配置自检通过")
        return 0

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print("配置校验未通过，缺失/问题清单：")
        print(str(e))
        return 1

    replay_file = args.replay or cfg.get("app", {}).get("replay_file") or None
    if replay_file:
        if not Path(replay_file).exists():
            print(f"[错误] 重放文件不存在: {replay_file}")
            return 1
        if args.speed <= 0:
            print("[错误] --speed 必须为正数")
            return 1

    app_cfg = cfg.get("app", {})
    setup_logger(paths.LOGS_DIR, app_cfg.get("log_level", "info"))
    stats = StatsCollector()

    print_config_summary(cfg)
    log = get_logger("main")
    log.info("startup", {"config": args.config, "replay": replay_file or None,
                         "speed": args.speed if replay_file else None})

    try:
        asyncio.run(run_main_loop(cfg, stats, echo=args.echo,
                                  replay_file=replay_file, speed=args.speed,
                                  open_browser=True))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"[错误] 运行失败: {type(e).__name__}: {e}")
        return 1

    log.info("shutdown", stats.summary())
    print(stats.format_summary())
    print("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
