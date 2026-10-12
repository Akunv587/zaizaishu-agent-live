# -*- coding: utf-8 -*-
"""ui_shots.py —— 无头 Edge + CDP 截取控制台各页签（构建期验收用）。

为什么不用 Playwright：本机已装 Edge（WebView2 运行时），直接用它的 CDP 即可，
零额外依赖 —— 只用标准库 + venv 里已有的 `websockets`。

它会自己起一个静态服务（默认 127.0.0.1:8799，指向 `webui/`），所以不必先启动主程序。
页面会因连不上 WebSocket 而显示「待接入」占位，但**配色、品牌、图标、布局**全部可验。

用法：
    python scripts/ui_shots.py                     # 截 5 个页签 + 2 张特写
    python scripts/ui_shots.py --view 1366x768     # 指定视口（V5.6 两档都要跑）
    python scripts/ui_shots.py --stub-ws           # 装 WS 替身：查智能体页 V5.1/V5.2/V5.6
    python scripts/ui_shots.py --out logs/shots    # 自定义输出目录
    python scripts/ui_shots.py --url http://127.0.0.1:8766/   # 用真跑起来的控制台

产出：<out>/1-直播中枢.png 等，默认 `logs/_ui_shots/`（`.gitignore` 已忽略）。

关于 `--stub-ws`：页面脚本是 IIFE，内部渲染函数从外部拿不到；所以在工具里另写一份
DOM 拼装是"验工具而不是验产品"。`--stub-ws` 改为在页面脚本**之前**装一个 WebSocket
替身，页面照常 `new WebSocket` / `send` / `onmessage`，帧由工具喂——被验证的就是
真实的消息分发与渲染代码。它会顺手断言：聚合卡数值 == 列表条数、判断依据与素材引用
是否上屏、以及 V5.6 的三处横向溢出，任一不过退出码 1。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEBUI = ROOT / "webui"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

PORT = 8799
CDP_PORT = 9333
VIEW = (1680, 1050)

# 每个页签：文件名、点击的按钮 id
TABS = [
    ("1-直播中枢", "tabMain"),
    ("2-音色工坊", "tabVoice"),
    ("3-话术管理", "tabScript"),
    ("4-智能体", "tabAgent"),
    ("5-系统配置", "tabConfig"),
]

# ── 「智能体」页的验收夹具 ──
# 静态服务下没有后端，页面脚本又是 IIFE（内部函数拿不到），所以**不能**在工具里另写一份
# DOM 拼装——那样验的是工具，不是产品。正解是在页面脚本之前装一个 **WebSocket 替身**：
# 页面照常 `new WebSocket` / `send` / `onmessage`，但帧由我们喂。于是被验证的是
# 真实的消息分发 + 渲染代码（V5.1 / V5.2 / V5.6 都要靠它）。
_SAMPLE_TS = "2026-09-22T23:40:0%d.123"


def _rec(i: int, **over) -> dict:
    """一条决策日志记录，字段与 `AgentBrain._record()` 同形状。

    刻意带全字段：夹具缺字段时页面会渲染成 `情绪：— → —`，看着像页面 bug，
    实际是夹具没喂真形状——"喂真产物"这条规矩对夹具同样适用。
    """
    base = {
        "ts": _SAMPLE_TS % i, "type": "chat", "user": "观众%d" % i,
        "event_line": "", "tier": "", "should_reply": True, "priority": 1,
        "interrupt": False, "tone": "热情", "reason": "", "reply_text": "",
        "emotion_raw": "happy", "emotion": "happy", "emotion_event": "",
        "speed": 1.0, "memory_note": "", "degrade": "", "latency_ms": 1200 + i * 37,
        "model": "deepseek-v4-flash", "recall": [], "prerecorded": False, "test": False,
    }
    base.update(over)
    return base


AGENT_FIXTURE_RECORDS = [
    _rec(0, type="chat", event_line="观众0：这游戏怎么玩",
         reason="有效提问，且这场还没回答过同类问题",
         reply_text="兄弟这个游戏点小手柄下载就能玩，挂着就行",
         recall=["sample:a3f1"], prerecorded=True),
    _rec(1, type="gift", tier="mid", priority=0, interrupt=True, tone="激动",
         event_line="老铁 送出 玫瑰 ×1（价值 100），属于中额礼物",
         reason="中额礼物，值得即时道谢并插队", reply_text="谢谢老铁的玫瑰！",
         emotion_raw="surprised", emotion="surprised"),
    _rec(2, should_reply=False, priority=2, reply_text="",
         event_line="观众2：主播几点下播",
         reason="闲聊性提问，回复价值低，且在防复读窗口内", tone="平淡"),
    _rec(3, type="follow", tone="亲切", event_line="观众3 关注了直播间",
         reason="新关注，报个名字能留住人", reply_text="欢迎观众3关注～", prerecorded=True),
    _rec(4, type="enter", should_reply=False, priority=2, reply_text="", tone="平淡",
         event_line="观众4 进入了直播间", reason="进场人数多，逐条欢迎会刷屏"),
    _rec(5, type="fansclub", priority=0, degrade="plain", latency_ms=2400,
         emotion_raw="fluent", emotion="happy", emotion_event="emotion_onsite",
         event_line="观众5 加入了粉丝团，附言：来了",
         reason="粉丝团是强信号，优先回应", reply_text="欢迎进团！"),
]
# 与上面 6 条自洽：should_reply 为真的是 4 条（0 / 1 / 3 / 5）
AGENT_FIXTURE_SUMMARY = {
    "total": 6, "test_skipped": 2, "replied": 4, "no_reply": 2, "no_reply_ratio": 33.3,
    "avg_latency_ms": 1485,
    "by_type": {"chat": 2, "gift": 1, "follow": 1, "enter": 1, "fansclub": 1},
    "by_type_ratio": {"chat": 33.3, "gift": 16.7, "follow": 16.7, "enter": 16.7,
                      "fansclub": 16.7},
    "degraded": 1, "prerecorded": 2, "first_ts": _SAMPLE_TS % 0, "last_ts": _SAMPLE_TS % 5,
}

# ── M6 反馈采集：直播中枢消息流里那条 AI 回复（带 feedback → 应长出三个按钮） ──
# 形状 = `router._feedback_context()` 的产物，也就是后端随消息流下发的真东西
FEED_FIXTURE = [{
    "kind": "play_start", "ts": 1790049300.0, "source": "chat", "priority": 1,
    "text": "兄弟这个游戏点小手柄下载就能玩，挂着就行", "tier": "",
    "merged_count": 1, "test": False,
    "feedback": {
        "event_type": "chat", "user": "阿强", "content": "这游戏怎么玩",
        "gift_name": "", "gift_count": 1, "tone": "热情",
        "reason": "有效提问，且这场还没回答过同类问题",
        "reply": "兄弟这个游戏点小手柄下载就能玩，挂着就行",
    },
}, {
    # 对照组：没有 feedback 的条目（rules / 模板链路）→ 不该长出按钮
    "kind": "play_start", "ts": 1790049305.0, "source": "schedule", "priority": 2,
    "text": "喜欢主播的点点关注，不迷路", "tier": "", "merged_count": 1, "test": False,
}]

# ── M6 反馈进化：四项指标 + 原因标签分布（形状 = FeedbackStore.stats()） ──
FB_STATS_FIXTURE = {
    "pid": "p_c2820e66", "total": 14, "usable": 9, "adopted": 7, "rewritten": 2,
    "rejected": 5, "week_added": 11, "adopt_rate": 58.3,
    "reasons": {"太长": 3, "跑题": 1, "重复": 1}, "max_examples": 300,
}

# ── M6 对话调参：面板初始状态（形状 = SessionTuner.state()） ──
# 注意 `current` 的键跟 SessionTuner.current() 一一对应（页面按这些键渲染"当前配置"），
# 少一个键页面就渲染出一条 "—"，看着像页面 bug 其实是夹具形状不对（HANDOVER §6.6 第 50 条）。
SESSION_STATE_FIXTURE = {
    "available": True,
    "reason": "",
    "fields": [
        {"key": "style", "label": "人设语气", "scope": "profile", "unit": "",
         "hint": "一句话说话风格（不超过 80 字）"},
        {"key": "max_reply_chars", "label": "回复字数上限", "scope": "profile",
         "unit": "字", "hint": "20 ~ 60 字；超过会被截断"},
        {"key": "interval_min", "label": "口播间隔", "scope": "config",
         "unit": "分钟", "hint": "1 ~ 60 分钟"},
        {"key": "topics_ai", "label": "话题条数", "scope": "profile", "unit": "条",
         "hint": "只增不减，删除请去话术管理页"},
        {"key": "samples_ai", "label": "话术样本", "scope": "profile", "unit": "条",
         "hint": "只增不减，删除请去话术管理页"},
        {"key": "game_intro", "label": "游戏介绍", "scope": "profile", "unit": "",
         "hint": "一句话玩法亮点"},
    ],
    "current": {
        "pid": "p_c2820e66", "style": "口语化、接地气，网感强，偏哥们聊天式",
        "max_reply_chars": 40, "interval_min": 7, "topics_ai": 6, "samples_ai": 3,
        "game_intro": "MMORPG 手游，讲职业选择与搬砖玩法", "game_name": "《示例游戏》",
    },
    "snapshots": [
        {"id": "ss_demo02", "ts": 1790049500, "request": "回复短一点",
         "reply": "已把回复字数上限调到 30 字",
         "changes": [{"field": "回复字数上限", "old": "40 字", "new": "30 字",
                      "scope": "profile", "note": "存档"}]},
        {"id": "ss_demo01", "ts": 1790048000, "request": "口播太频繁了",
         "reply": "已把口播间隔改到 12 分钟",
         "changes": [{"field": "口播间隔", "old": "7 分钟", "new": "12 分钟",
                      "scope": "config", "note": "config.yaml + 存档轮播"}]},
    ],
}

_WS_STUB_TEMPLATE = r"""
(() => {
  // 页面里抛在 setTimeout / 事件回调里的异常不会经 CDP 的 evaluate 冒出来，
  // 只会静默留在控制台——先收着，查验时一并打印（"注入没反应"多半就是这个）
  window.__errs = [];
  window.addEventListener('error', (e) => window.__errs.push(String(e && e.message)));
  window.addEventListener('unhandledrejection',
    (e) => window.__errs.push('reject: ' + String(e && e.reason)));

  const RECORDS = __RECORDS__;
  const SUMMARY = __SUMMARY__;
  const FEED = __FEED__;
  const FB_STATS = __FB_STATS__;
  const SESSION_STATE = __SESSION_STATE__;
  window.__feed = FEED;      // 查验脚本主动喂一帧 feed 用
  window.__sessionState = SESSION_STATE;   // 查验脚本用完"置灰态"后恢复用

  // 按动作回帧；`ag_playground` 把页面发来的表单字段**原样回显**，
  // 这样截图里就能看出「表单 → 载荷 → 渲染」这条链接得对不对
  const reply = (action, msg) => {
    if (action === 'ag_status') return { type: 'agent', data: {
      mode: 'agent', enabled: true, llm_configured: true, available: true, no_expire: true,
      configured_enabled: true, downgraded: false,
      model: 'deepseek-v4-flash', session: { events: 14, spoken: 12, notes: 2 },
      settings: { retrieve: { layer: 'l2', top_k: 3 },
                  memory: { max_events: 50, max_spoken: 20 },
                  emotion: { enabled: true, onsite_fallback: true },
                  decision_log: true } } };
    if (action === 'ag_logs') return { type: 'agent_logs', data: {
      available: true, records: RECORDS, summary: SUMMARY,
      path: 'logs/agent-20260922.jsonl', note: '' } };
    if (action === 'ag_playground') {
      const batch = msg.batch || null;
      const ev = (batch ? Object.assign({}, batch, { type: batch.type }) : null)
                 || (msg.events && msg.events[0]) || {};
      const n = batch ? Math.max(1, Number(batch.count) || 1) : 1;
      const kind = ev.type || 'chat';
      const user = ev.user || '阿强';
      const line = kind === 'gift'
        ? user + ' 送出 ' + (ev.gift_name || '礼物') + ' ×1（价值 ' + (ev.gift_value || 0) + '），属于中额礼物'
        : user + '：' + (ev.content || '这游戏怎么玩');
      const results = [];
      for (let i = 0; i < n; i++) {
        results.push({
          index: i, error: '', type: kind, user: user,
          event_line: n > 1 ? line + '（第 ' + (i + 1) + ' 条）' : line,
          should_reply: true, priority: 1, interrupt: false, tone: '热情',
          reason: '有效提问，未被回答过（桩服务：表单字段已原样回显）',
          reply_text: '兄弟这个游戏点小手柄下载就能玩，挂着就行',
          emotion_raw: 'happy', emotion: 'happy', emotion_event: '', speed: 1.0,
          memory_note: '', degrade: '', latency_ms: 1180,
          model: 'deepseek-v4-flash', recall: ['sample:a3f1'],
          prerecorded: true, test: true,
        });
      }
      const byType = {}; byType[kind] = n;
      return { type: 'agent_playground', ok: true, data: {
        ok: true, message: '', results: results,
        summary: { count: n, replied: n, no_reply: 0, failed: 0,
                   by_type: byType, avg_latency_ms: 1180 } } };
    }
    if (action === 'vc_preview') return { type: 'vc', data: {
      op: 'preview_done', ok: true, audio_url: '', chars: String(msg.text || '').length } };
    if (action === 'ag_feedback_stats') return { type: 'agent_feedback_stats', ok: true,
      data: { available: true, stats: FB_STATS, note: '' } };
    // 采集回帧：把统计一起捎回去（真实后端就是这么回的），页面据此标记该行 + 刷新进化区
    if (action === 'ag_feedback') return { type: 'agent_feedback_done', ok: true,
      data: { ok: true, message: '桩服务：已记录 ' + msg.kind,
              record: { id: 'fb_stub01' }, feedback_id: 'fb_stub01', stats: FB_STATS } };
    if (action === 'ag_session_state') return { type: 'agent_session_state', ok: true,
      data: Object.assign({}, SESSION_STATE) };
    if (action === 'ag_session_send') {
      // 桩不回"真解析"，只把发出去的原文回显进回复：这样截图能看出
      // 「输入框 → WS 载荷 → 会话气泡」这条链接得对不对（载荷本身由查验脚本断言）
      const text = String(msg.text || '');
      const changes = text.indexOf('间隔') >= 0 || text.indexOf('频繁') >= 0
        ? [{ field: '口播间隔', old: '7 分钟', new: '12 分钟', scope: 'config',
             note: 'config.yaml + 存档轮播' }]
        : [{ field: '回复字数上限', old: '40 字', new: '30 字', scope: 'profile',
             note: '存档' }];
      return { type: 'agent_session_reply', ok: true, data: {
        kind: 'send', ok: true, message: '', reply: '收到：' + text,
        changes: changes, errors: [], restored: [], snapshot_id: 'ss_stub01' } };
    }
    if (action === 'ag_session_undo') return { type: 'agent_session_reply', ok: true,
      data: { kind: 'undo', ok: true, message: '已撤销：口播间隔 回到 7 分钟',
              changes: [], errors: [], restored: ['口播间隔 回到 7 分钟'],
              snapshot_id: String(msg.id || 'ss_stub01') } };
    return null;
  };

  class FakeWS {
    constructor(url) {
      this.url = String(url);
      this.readyState = 0;
      this.onopen = this.onmessage = this.onclose = this.onerror = null;
      window.__fakeWS = this;
      window.__sent = [];
      setTimeout(() => { this.readyState = 1; if (this.onopen) this.onopen(); }, 40);
    }
    send(raw) {
      let msg = null;
      try { msg = JSON.parse(raw); } catch (e) { return; }
      window.__sent.push(msg);
      const frame = reply(msg && msg.action, msg || {});
      if (frame) setTimeout(() => this._recv(frame), 25);
    }
    push(frame) { this._recv(frame); }        // 工具侧主动喂帧（查验用）
    _recv(frame) { if (this.onmessage) this.onmessage({ data: JSON.stringify(frame) }); }
    close() { this.readyState = 3; if (this.onclose) this.onclose(); }
    addEventListener() {}
    removeEventListener() {}
  }
  FakeWS.CONNECTING = 0; FakeWS.OPEN = 1; FakeWS.CLOSING = 2; FakeWS.CLOSED = 3;
  Object.defineProperty(window, 'WebSocket',
    { value: FakeWS, writable: true, configurable: true });
})()
"""


def build_ws_stub() -> str:
    """把夹具塞进 WS 替身源码（页面脚本之前注入，见 capture）。"""
    return (_WS_STUB_TEMPLATE
            .replace("__RECORDS__", json.dumps(AGENT_FIXTURE_RECORDS, ensure_ascii=False))
            .replace("__SUMMARY__", json.dumps(AGENT_FIXTURE_SUMMARY, ensure_ascii=False))
            .replace("__FEED__", json.dumps(FEED_FIXTURE, ensure_ascii=False))
            .replace("__FB_STATS__", json.dumps(FB_STATS_FIXTURE, ensure_ascii=False))
            .replace("__SESSION_STATE__",
                     json.dumps(SESSION_STATE_FIXTURE, ensure_ascii=False)))


# 「智能体」页重点查验（V5.1 / V5.2）：聚合卡数值 vs 列表条数、判断依据与素材引用是否上屏
AGENT_CHECK_JS = r"""
(() => {
  const first = document.querySelector('#agpLog .agp-line');
  if (first) first.click();                       // 展开第一条
  const vals = [...document.querySelectorAll('#agpKpis .agp-kpi .v')]
                 .map(e => e.textContent.replace(/[^0-9.]/g, ''));   // 去掉单位（条 / % ）
  const rows = document.querySelectorAll('#agpLog .agp-item').length;
  const d = document.querySelector('#agpLog .agp-item .agp-detail');
  const txt = d ? d.textContent : '';
  const out = document.querySelectorAll('#agpOut .agp-out-item').length;
  return JSON.stringify({
    kpiDecisions: vals[0] || '', kpiLatency: vals[1] || '', kpiNoReply: vals[2] || '',
    logRows: rows,
    detailOpen: !!(d && d.offsetParent !== null),
    hasReason: txt.indexOf('判断依据') >= 0,
    hasReply: !!(d && d.querySelector('.rp')),
    hasRecall: txt.indexOf('sample:') >= 0,
    hasEmotion: txt.indexOf('happy') >= 0 && txt.indexOf('— → —') < 0,
    outItems: out,
    outHasReason: (document.querySelector('#agpOut .agp-out-item') || {}).textContent
                    ?.indexOf('判断依据') >= 0,
    barSegments: document.querySelectorAll('#agpKpis .agp-dist-bar i').length,
    legendItems: document.querySelectorAll('#agpKpis .agp-legend span').length,
    wsState: window.__fakeWS ? window.__fakeWS.readyState : null,
    errs: window.__errs || [],
  });
})()
"""

# V5.4 试听接线：走真实 snapshot 路径把「当前直播音色」灌进页面（页面是从
# `snapshot.config.voice_id` 取它的），再点试玩结果上的「用当前音色试听」，
# 看它发出去的是不是一条 `vc_preview`（复用音色工坊那条已经测过的链路）。
# 放在所有截图之后跑：snapshot 帧会把整页状态填满，不能污染其它页签的截图。
V54_CHECK_JS = r"""
(() => {
  window.__fakeWS.push({ type: 'snapshot', ts: Date.now() / 1000,
    paused: false, playing: false, live: false, uptime_s: 0,
    config: { voice_id: 'zs-demo-voice', minimax_configured: true,
              llm_configured: true, llm_key_masked: 'sk-****demo' } });
  document.getElementById('tabAgent').click();
  const b = document.querySelector('#agpOut [data-speak]');
  if (!b) return JSON.stringify({ err: '试玩结果里没有试听按钮' });
  const before = (window.__sent || []).length;
  b.click();
  const sent = (window.__sent || []).slice(before)
                 .filter(m => m && m.action === 'vc_preview');
  const m = sent[0] || null;
  return JSON.stringify({
    btnDisabled: !!b.disabled,
    previews: sent.length,
    voiceId: m ? m.voice_id : '',
    textHead: m ? String(m.text || '').slice(0, 8) : '',
    note: document.getElementById('agpNote').textContent,
    errs: window.__errs || [],
  });
})()
"""

# M6 反馈采集（V6.1 / V6.2 / V6.3 的页面层）：喂一帧带 feedback 的消息流，
# 走真实分发与渲染，再点真实 DOM（页面是 IIFE，内部函数拿不到——点 DOM 才验得到东西）。
# 每段检查都重新喂一帧："采纳成功后该行按钮会被替换成「已采纳」标记"，不重喂就没得点了。
FEED_CHECK_JS = r"""
(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const out = {};
  const ws = window.__fakeWS;
  document.getElementById('tabMain').click();
  const fresh = async () => {
    ws.push({ type: 'feed', data: window.__feed });
    await sleep(120);
    const rows = [...document.querySelectorAll('#feed .row.play')].filter(p => p.querySelector('.fb'));
    return rows[rows.length - 1] || null;
  };

  ws.push({ type: 'feed', data: window.__feed });
  await sleep(150);
  const plays = [...document.querySelectorAll('#feed .row.play')];
  out.plays = plays.length;                                   // 夹具两条
  out.rowsWithButtons = plays.filter(p => p.querySelector('.fb')).length;   // 只有带 feedback 的那条
  out.buttons = document.querySelectorAll('#feed .row.play .fb .fb-btn').length;
  out.markCount = document.querySelectorAll('#feed .row.play .fb-mark').length;

  // ① 采纳：点对勾 → 应发出恰好一条 ag_feedback(adopt)，并就地变成「已采纳」
  let row = await fresh();
  if (!row) return JSON.stringify(Object.assign(out, { err: '消息流没渲染出带按钮的行' }));
  let before = (window.__sent || []).length;
  row.querySelector('.fb-ok').click();
  await sleep(120);
  const s1 = (window.__sent || []).slice(before).filter(m => m && m.action === 'ag_feedback');
  out.adoptSent = s1.length;
  out.adoptKind = s1[0] ? s1[0].kind : '';
  out.adoptReply = s1[0] && s1[0].feedback ? s1[0].feedback.reply : '';
  out.adoptEvent = s1[0] && s1[0].feedback ? s1[0].feedback.content : '';
  out.adoptMark = (row.querySelector('.fb-mark') || {}).textContent || '';

  // ② 弃用：弹窗必须列全四个原因；不选原因直接确认要被拦住
  row = await fresh();
  row.querySelector('.fb-no').click();
  await sleep(80);
  const modal = document.getElementById('fbModal');
  out.rejectModalOpen = !modal.hidden;
  out.rejectChips = document.querySelectorAll('#fbModalBody .fb-chip').length;
  before = (window.__sent || []).length;
  document.getElementById('fbModalOk').click();
  await sleep(80);
  out.emptyRejectSent = (window.__sent || []).slice(before)
                          .filter(m => m && m.action === 'ag_feedback').length;
  out.emptyRejectStillOpen = !modal.hidden;                    // 被拦住 → 弹窗保持打开
  out.emptyRejectHint = (document.querySelector('#toastRoot .toast') || {}).textContent || '';
  document.querySelector('#fbModalBody .fb-chip').click();
  before = (window.__sent || []).length;
  document.getElementById('fbModalOk').click();
  await sleep(120);
  const s2 = (window.__sent || []).slice(before).filter(m => m && m.action === 'ag_feedback');
  out.rejectSent = s2.length;
  out.rejectKind = s2[0] ? s2[0].kind : '';
  out.rejectReasons = s2[0] ? (s2[0].reasons || []).join('/') : '';
  out.rejectClosed = modal.hidden;

  // ③ 改写：输入框预填原句，改后提交带 text
  row = await fresh();
  row.querySelector('.fb-ed').click();
  await sleep(80);
  const ta = document.getElementById('fbText');
  out.rewriteModalOpen = !modal.hidden;
  out.rewritePrefilled = ta ? ta.value : '';
  if (ta) ta.value = '改过的句子';
  before = (window.__sent || []).length;
  document.getElementById('fbModalOk').click();
  await sleep(120);
  const s3 = (window.__sent || []).slice(before).filter(m => m && m.action === 'ag_feedback');
  out.rewriteSent = s3.length;
  out.rewriteText = s3[0] ? s3[0].text : '';

  out.errs = window.__errs || [];
  return JSON.stringify(out);
})()
"""

# M6 反馈进化区：四项指标 + 原因标签分布色条（走 ag_feedback_stats 真实帧）
FB_STATS_CHECK_JS = r"""
(async () => {
  document.getElementById('tabAgent').click();
  await new Promise(r => setTimeout(r, 260));
  return JSON.stringify({
    label: document.getElementById('agpFbCount').textContent,
    kpis: [...document.querySelectorAll('#agpFbKpis .agp-kpi')]
            .map(e => e.textContent.replace(/\s+/g, '')),
    bars: document.querySelectorAll('#agpFbReasons .agp-fb-bar i').length,
    legend: document.querySelectorAll('#agpFbReasons .agp-fb-item').length,
    errs: window.__errs || [],
  });
})()
"""

# M6 对话调参查验：面板渲染 / 真实发送路径 / 撤销 / 无 Key 置灰（V6.7）
AGT_CHECK_JS = r"""
(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const out = {};
  const ws = window.__fakeWS;
  const txt = (el) => (el ? el.textContent.replace(/\s+/g, '') : '');

  document.getElementById('tabAgent').click();
  await sleep(260);

  // ① 初始状态：当前配置 5 行 + 快照列表 + 输入可用
  const dt = [...document.querySelectorAll('#agtPrev dt')].map(e => e.textContent);
  const dd = [...document.querySelectorAll('#agtPrev dd')].map(e => e.textContent);
  out.prevRows = dt.length;
  out.prevLabels = dt.join('/');
  out.prevValues = dd.join(' | ');
  out.stateText = txt(document.getElementById('agtState'));
  out.offHidden = document.getElementById('agtOff').hidden;
  out.bodyHidden = document.getElementById('agtBody').hidden;
  out.inputDisabled = document.getElementById('agtInput').disabled;
  out.tips = document.querySelectorAll('.agt-tip').length;
  out.undoRows = document.querySelectorAll('#agtUnd .agt-und').length;
  out.undoCnt = txt(document.getElementById('agtUndCnt'));
  out.undoBtnText = txt(document.querySelector('#agtUnd button[data-undo]'));

  // ② 点提示词「口播太频繁了」→ 走真实发送路径，回帧后气泡里要有「字段：旧值 → 新值」
  const before = (window.__sent || []).length;
  const tip = [...document.querySelectorAll('.agt-tip')]
                .find(b => b.textContent.indexOf('频繁') >= 0) || document.querySelector('.agt-tip');
  if (tip) tip.click();
  await sleep(240);
  const sent = (window.__sent || []).slice(before).filter(m => m && m.action === 'ag_session_send');
  out.sendCount = sent.length;
  out.sendText = sent[0] ? sent[0].text : '';
  out.meBubble = txt(document.querySelector('#agtLog .agt-msg.me .bub'));
  out.diffText = txt(document.querySelector('#agtLog .agt-msg .diff'));
  out.stillPending = document.getElementById('agtLog').textContent.indexOf('正在理解') >= 0;
  out.sendReenabled = !document.getElementById('agtSend').disabled;

  // ③ 点撤销 → 发出 ag_session_undo(id)，回帧后气泡显示「已撤销」
  const before2 = (window.__sent || []).length;
  const ub = document.querySelector('#agtUnd button[data-undo]');
  if (ub) ub.click();
  await sleep(240);
  const usent = (window.__sent || []).slice(before2).filter(m => m && m.action === 'ag_session_undo');
  out.undoSent = usent.length;
  out.undoId = usent[0] ? usent[0].id : '';
  out.undoMsg = document.getElementById('agtLog').textContent.indexOf('已撤销') >= 0;

  // ④ V6.7：无 Key 时必须整块置灰并说明原因（不是隐藏、不是空着）
  ws.push({ type: 'agent_session_state', ok: false, data: {
    available: false, fields: [], current: {}, snapshots: [],
    reason: '未配置 LLM Key：对话调参需要大模型把口语要求翻译成配置改动' } });
  await sleep(180);
  out.offShown = !document.getElementById('agtOff').hidden;
  out.offReason = txt(document.getElementById('agtOff'));
  out.bodyHiddenOff = document.getElementById('agtBody').hidden;
  out.inputDisabledOff = document.getElementById('agtInput').disabled;
  out.sendDisabledOff = document.getElementById('agtSend').disabled;
  out.tipsDisabledOff = [...document.querySelectorAll('.agt-tip')]
                          .every(b => b.disabled);

  // 恢复可用态：后面的截图要展示正常面板
  ws.push({ type: 'agent_session_state', ok: true, data: window.__sessionState });
  await sleep(180);
  out.restored = !document.getElementById('agtBody').hidden;
  out.errs = window.__errs || [];
  return JSON.stringify(out);
})()
"""

# V5.6 溢出探针：窗口宽 / 文档宽 / 页面容器宽 / 单条内容最大横向溢出，四个数都别超
OVERFLOW_JS = """(() => {
  const de = document.documentElement;
  const pg = document.getElementById('viewAgent');
  const items = [...document.querySelectorAll(
    '#viewAgent .agp-card, #viewAgent .agp-kpi, #viewAgent .agp-line, #viewAgent .agp-out-item')];
  const worst = items.length ? Math.max(...items.map(e => e.scrollWidth - e.clientWidth)) : 0;
  return JSON.stringify({
    vw: window.innerWidth, vh: window.innerHeight,
    docW: de.scrollWidth, bodyW: document.body.scrollWidth,
    pgClientW: pg.clientWidth, pgScrollW: pg.scrollWidth,
    pgClientH: pg.clientHeight, pgScrollH: pg.scrollHeight,
    worstItemOverflow: worst,
  });
})()
"""

# 注入演示：把状态标记（✓/✗/⚠/💡）以真实 SVG mask 形态渲染出来
DEMO_JS = """
(() => {
  const set = (id, cls, text) => {
    const e = document.getElementById(id);
    if (e) { e.className = cls; e.textContent = text; }
  };
  set('llmKeyFormat', 'llm-key-format st-mark st-mark-ok ok', '格式正确（DeepSeek）');
  set('llmSaveNote', 'llm-save-note st-mark st-mark-ok ok', '配置已保存并热生效');

  const h = document.getElementById('llmTestHistory');
  const l = document.getElementById('llmTestHistoryList');
  if (h && l) {
    h.hidden = false;
    l.innerHTML =
      '<div class="llm-test-history-item"><span class="h-time">21:08:11</span>' +
      '<span class="h-status st-mark st-mark-ok ok"></span>' +
      '<span class="h-detail">连接成功 · deepseek-v4-flash</span></div>' +
      '<div class="llm-test-history-item"><span class="h-time">21:07:02</span>' +
      '<span class="h-status st-mark st-mark-err fail"></span>' +
      '<span class="h-detail">401 鉴权失败</span></div>';
  }
  const p = document.getElementById('llmTestPanel');
  const i = document.getElementById('llmTestIcon');
  const b = document.getElementById('llmTestBody');
  if (p && i && b) {
    p.hidden = false; p.className = 'llm-test-panel success';
    i.textContent = '';
    b.innerHTML = '<div class="llm-test-label">连接成功</div>' +
      '<div class="llm-test-detail">延迟 412ms · 返回 2 tokens</div>' +
      '<div class="llm-test-advice st-mark st-mark-tip">建议把 max_reply_chars 调到 40 以内，当前 40 已达上限。</div>';
  }
  document.querySelector('.llm-test-panel')?.scrollIntoView({block: 'center'});
  return true;
})()
"""


def find_edge() -> str | None:
    for c in EDGE_CANDIDATES:
        if os.path.exists(c):
            return c
    return shutil.which("msedge")


def start_static_server(directory: Path, port: int) -> socketserver.TCPServer:
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=str(directory), **kw)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        # 2026-09-23 踩坑（HANDOVER §6.7）：ThreadingMixIn 默认 daemon_threads=False，
        # 每个请求跑在一条**非守护**线程里。收尾时先杀浏览器，若某个请求线程正好
        # 卡在 socket 写（浏览器不读了），解释器退出会永久 join 它 —— 现象是
        # 打印完「输出目录」脚本却再也不退出，后台任务一直挂着。
        daemon_threads = True
        block_on_close = False

    srv = Server(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def reachable(url: str, timeout: float = 2.0) -> bool:
    # 绕开系统代理：本机回环不该走代理（本机常见 127.0.0.1:xxxx 代理）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


async def capture(url: str, out: Path, view: tuple[int, int] = VIEW,
                  stub_ws: bool = False) -> int:
    from websockets.asyncio.client import connect

    edge = find_edge()
    if not edge:
        print("找不到 msedge.exe，请检查 Edge 安装", file=sys.stderr)
        return 2

    profile = Path(tempfile.gettempdir()) / ("ui_shots_edge_profile_%d" % os.getpid())
    shutil.rmtree(profile, ignore_errors=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def cdp_http(path: str):
        with opener.open("http://127.0.0.1:%d%s" % (CDP_PORT, path), timeout=10) as r:
            return json.loads(r.read().decode())

    proc = subprocess.Popen(
        [edge, "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", "--hide-scrollbars",
         "--force-device-scale-factor=1",
         "--window-size=%d,%d" % view,
         "--remote-debugging-port=%d" % CDP_PORT,
         "--user-data-dir=%s" % profile,
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        for _ in range(60):
            try:
                cdp_http("/json/version")
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("CDP 未就绪", file=sys.stderr)
            return 3

        page = next(t for t in cdp_http("/json/list") if t["type"] == "page")
        mid = [0]
        async with connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:

            async def cmd(method: str, **params):
                mid[0] += 1
                await ws.send(json.dumps({"id": mid[0], "method": method, "params": params}))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                    if msg.get("id") == mid[0]:
                        if "error" in msg:
                            raise RuntimeError("%s -> %s" % (method, msg["error"]))
                        return msg.get("result", {})

            async def js(expr: str):
                r = await cmd("Runtime.evaluate", expression=expr,
                              returnByValue=True, awaitPromise=True)
                res = r.get("result", {}) or {}
                # 页面里抛异常时 `result` 只带 subtype:"error"，没有 value ——
                # 不显式检查就会静默拿到 None，注入失败看起来像"注入生效了但没数据"
                if res.get("subtype") == "error" or r.get("exceptionDetails"):
                    det = r.get("exceptionDetails", {}) or {}
                    raise RuntimeError("页面 JS 异常：%s" % (
                        (det.get("exception") or {}).get("description")
                        or det.get("text") or res.get("description") or "未知"))
                return res.get("value")

            async def shot(name: str):
                await asyncio.sleep(0.7)
                r = await cmd("Page.captureScreenshot", format="png",
                              captureBeyondViewport=False)
                p = out / ("%s.png" % name)
                p.write_bytes(base64.b64decode(r["data"]))
                print("  截图 %-24s %8d bytes" % (name, p.stat().st_size))

            await cmd("Page.enable")
            await cmd("Runtime.enable")
            await cmd("Emulation.setDeviceMetricsOverride",
                      width=view[0], height=view[1], deviceScaleFactor=1, mobile=False)
            if stub_ws:
                # 必须在 navigate 之前：页面脚本一跑就会 new WebSocket
                await cmd("Page.addScriptToEvaluateOnNewDocument", source=build_ws_stub())
            await cmd("Page.navigate", url=url)

            async def wait_ready(timeout: float = 30.0) -> str:
                """导航后不能只 sleep（2026-09-23 踩坑，见 HANDOVER §6.7）：
                文档替换的那一瞬间 `document.documentElement` 是 null，
                此时 `getComputedStyle(document.documentElement)` 直接抛
                TypeError，页面标题/品牌行全空 —— 看起来像"页面坏了"，
                其实是抓拍早了一拍。改为轮询到真正就绪。
                返回空串表示就绪，否则返回最后一次拿到的状态串。
                """
                deadline = time.monotonic() + timeout
                last = "（一帧都没取到）"
                while time.monotonic() < deadline:
                    try:
                        st = await js(
                            "document.readyState + '|' + (document.documentElement ? 1 : 0)"
                            " + '|' + (document.querySelector('.tab') ? 1 : 0)")
                    except RuntimeError as e:
                        last, st = str(e), None
                    if st == "complete|1|1":
                        return ""
                    if st:
                        last = st
                    await asyncio.sleep(0.2)
                return last

            # 撞上「上一次跑剩的浏览器进程还没退干净」时，页面会永远停在
            # loading（ready 状态 loading|0|0）—— 重新导航一次即可。
            # 这也是为什么 profile 目录要带 pid：同 profile 会「交接」给
            # 那个将死未死的旧进程，新页永远加载不出来。
            stuck = await wait_ready()
            if stuck:
                print("  首次导航未就绪（%s），重新导航一次" % stuck)
                await cmd("Page.navigate", url=url)
                stuck = await wait_ready()
                if stuck:
                    raise RuntimeError(
                        "页面 60s 内未就绪（最后状态：%s）—— 多半是上一次跑的 "
                        "Edge 进程没退干净抢了 profile，检查 tasklist 里有无 "
                        "带 --headless 的 msedge" % stuck)

            print("  页面标题:", await js("document.title"))
            print("  品牌行  :", await js(
                "document.querySelector('.brand .nm')?.innerText.replace(/\\n/g,' / ')"))
            print("  页签    :", await js(
                "[...document.querySelectorAll('.tab')].map(t=>t.textContent).join(' | ')"))
            print("  主题色  :", await js(
                "getComputedStyle(document.documentElement).getPropertyValue('--brand-primary').trim()"))

            async def check_overflow() -> list:
                """V5.6：文档与页面容器都不许横向溢出，页内元素也不许。"""
                m = json.loads(await js(OVERFLOW_JS))
                print("  溢出探针:", json.dumps(m, ensure_ascii=False))
                bad = []
                if max(m["docW"], m["bodyW"]) > m["vw"]:
                    bad.append("文档横向溢出 %d > 视口 %d" % (max(m["docW"], m["bodyW"]), m["vw"]))
                if m["pgScrollW"] > m["pgClientW"]:
                    bad.append("智能体页横向溢出 %d > %d" % (m["pgScrollW"], m["pgClientW"]))
                if m["worstItemOverflow"] > 0:
                    bad.append("页内元素横向溢出 %dpx" % m["worstItemOverflow"])
                return bad

            async def check_agent() -> list:
                """V5.1 / V5.2：走页面自己的表单送入一条，再查渲染结果。

                用 `element.click()` 而不是内部函数：页面脚本是 IIFE，内部函数本就
                拿不到——而这恰好也是正确做法，验的是产品代码而非工具的复刻。
                """
                await js("document.getElementById('agpUser').value='阿强';"
                         "document.getElementById('agpContent').value='这游戏怎么玩';"
                         "document.getElementById('agpSend').click()")
                await asyncio.sleep(0.5)
                r = json.loads(await js(AGENT_CHECK_JS))
                print("  智能体页查验:", json.dumps(r, ensure_ascii=False))
                bad = []
                if r["errs"]:
                    bad.append("页面有未捕获异常：%s" % r["errs"][:3])
                if not r["barSegments"]:
                    bad.append("决策透视没收到 ag_logs 帧或没渲染（V5.2）")
                if not r["hasReason"]:
                    bad.append("决策透视未显示「判断依据」（V5.1）")
                if not r["hasReply"]:
                    bad.append("决策透视未显示回复正文（V5.1）")
                if not r["hasRecall"]:
                    bad.append("决策透视未显示素材引用（V5.1）")
                if not r["hasEmotion"]:
                    bad.append("情绪 / 语速没上屏（夹具缺字段，或渲染漏了）")
                if not r["detailOpen"]:
                    bad.append("点开一条后明细没有展开")
                if str(r["logRows"]) != str(r["kpiDecisions"]):
                    bad.append("聚合卡「本场决策数」%s != 列表条数 %s（V5.2）"
                               % (r["kpiDecisions"], r["logRows"]))
                if not r["outItems"] or not r["outHasReason"]:
                    bad.append("试玩结果未渲染出完整决策（V5.1）")
                if not r["barSegments"] or not r["legendItems"]:
                    bad.append("各类事件占比没有色条 / 图例（V5.2）")
                return bad

            bad = []

            async def check_feed_feedback() -> list:
                """V6.1 / V6.2 / V6.3 的页面层：三个按钮真的长出来、点下去发出对的载荷。"""
                r = json.loads(await js(FEED_CHECK_JS))
                print("  反馈采集查验:", json.dumps(r, ensure_ascii=False))
                bad = []
                if r.get("errs"):
                    bad.append("反馈采集查验时页面报错：%s" % r["errs"][:3])
                if r.get("rowsWithButtons") != 1:
                    bad.append("消息流带按钮的行数应为 1（只有智能体决策带 feedback），实际 %s"
                               % r.get("rowsWithButtons"))
                if r.get("buttons") != 3:
                    bad.append("那条回复旁应是 3 个图标按钮，实际 %s" % r.get("buttons"))
                if r.get("markCount"):
                    bad.append("未反馈的行不该有已反馈标记")
                if r.get("adoptSent") != 1 or r.get("adoptKind") != "adopt":
                    bad.append("点对勾没发出恰好一条 ag_feedback(adopt)（V6.1）")
                if not r.get("adoptReply") or not r.get("adoptEvent"):
                    bad.append("采纳载荷没带上「回复原文 / 事件内容」（V6.1）")
                if r.get("adoptMark") != "已采纳":
                    bad.append("采纳后该行没变成「已采纳」：%s" % r.get("adoptMark"))
                if not r.get("rejectModalOpen") or r.get("rejectChips") != 4:
                    bad.append("弃用弹窗没弹或原因标签不是 4 个（V6.2）：%s" % r.get("rejectChips"))
                if r.get("emptyRejectSent") != 0 or not r.get("emptyRejectStillOpen"):
                    bad.append("不选原因直接确认没被拦住（V6.2）：弹窗应保持打开并提示")
                if r.get("rejectSent") != 1 or r.get("rejectKind") != "reject":
                    bad.append("选了原因后没发出 ag_feedback(reject)（V6.2）")
                if not r.get("rejectReasons"):
                    bad.append("弃用载荷没带原因标签（V6.2）")
                if not r.get("rewriteModalOpen") or not r.get("rewritePrefilled"):
                    bad.append("改写弹窗没弹或输入框没预填原句（V6.3）")
                if r.get("rewriteSent") != 1 or r.get("rewriteText") != "改过的句子":
                    bad.append("改写提交没带上改后文本（V6.3）")
                return bad

            async def check_fb_stats() -> list:
                """反馈进化区：四项指标 + 原因标签色条都上屏。"""
                r = json.loads(await js(FB_STATS_CHECK_JS))
                print("  反馈进化查验:", json.dumps(r, ensure_ascii=False))
                bad = []
                if r.get("errs"):
                    bad.append("反馈进化查验时页面报错：%s" % r["errs"][:3])
                if len(r.get("kpis") or []) < 4:
                    bad.append("反馈进化区指标不足 4 项：%s" % r.get("kpis"))
                if "样本总数" not in "".join(r.get("kpis") or []):
                    bad.append("反馈进化区缺「样本总数」")
                if "采纳率" not in "".join(r.get("kpis") or []):
                    bad.append("反馈进化区缺「采纳率」")
                if not r.get("bars") or not r.get("legend"):
                    bad.append("原因标签分布没出色条 / 图例")
                return bad

            async def check_agent_session() -> list:
                """对话调参区：面板渲染 / 发送路径 / 撤销 / 无 Key 置灰（V6.7）。"""
                r = json.loads(await js(AGT_CHECK_JS))
                print("  对话调参查验:", json.dumps(r, ensure_ascii=False))
                bad = []
                if r.get("errs"):
                    bad.append("对话调参查验时页面报错：%s" % r["errs"][:3])
                if r.get("prevRows") != 5:
                    bad.append("「当前配置」应渲染 5 行，实际 %s（%s）"
                               % (r.get("prevRows"), r.get("prevLabels")))
                if "40 字" not in (r.get("prevValues") or ""):
                    bad.append("「当前配置」没显示回复字数上限：%s" % r.get("prevValues"))
                if "7 分钟" not in (r.get("prevValues") or ""):
                    bad.append("「当前配置」没显示口播间隔：%s" % r.get("prevValues"))
                if not r.get("offHidden") or r.get("bodyHidden"):
                    bad.append("可用状态下不该显示置灰块、应显示面板")
                if r.get("inputDisabled"):
                    bad.append("可用状态下输入框不该禁用")
                if r.get("tips") != 4:
                    bad.append("示例指令应有 4 条，实际 %s" % r.get("tips"))
                if r.get("undoRows") != 2 or r.get("undoCnt") != "2":
                    bad.append("可撤销列表应列出 2 条快照：%s / %s"
                               % (r.get("undoRows"), r.get("undoCnt")))
                if r.get("sendCount") != 1:
                    bad.append("点示例指令没发出恰好一条 ag_session_send")
                if r.get("sendText") != "口播太频繁了":
                    bad.append("发出去的指令不是点的那条：%s" % r.get("sendText"))
                if "口播太频繁了" not in (r.get("meBubble") or ""):
                    bad.append("我方气泡没显示发出去的原文：%s" % r.get("meBubble"))
                if "口播间隔：7分钟→12分钟" not in (r.get("diffText") or ""):
                    bad.append("回显不是「字段：旧值 → 新值」：%s" % r.get("diffText"))
                if r.get("stillPending"):
                    bad.append("回帧到了占位气泡「正在理解」还在（没被替换）")
                if not r.get("sendReenabled"):
                    bad.append("回帧后发送按钮没恢复可用")
                if r.get("undoSent") != 1 or r.get("undoId") != "ss_demo02":
                    bad.append("点撤销没发出 ag_session_undo(最新快照 id)：%s / %s"
                               % (r.get("undoSent"), r.get("undoId")))
                if not r.get("undoMsg"):
                    bad.append("撤销后聊天区没出现「已撤销」")
                # V6.7：无 Key 时置灰 + 说明原因，且话术管理表单不受影响（另一页，不在本查验）
                if not r.get("offShown") or not r.get("bodyHiddenOff"):
                    bad.append("无 LLM Key 时面板没置灰（V6.7）")
                if "LLMKey" not in (r.get("offReason") or ""):
                    bad.append("置灰时没说明原因（V6.7）：%s" % r.get("offReason"))
                if not r.get("inputDisabledOff") or not r.get("sendDisabledOff"):
                    bad.append("置灰时输入框 / 发送按钮应禁用（V6.7）")
                if not r.get("tipsDisabledOff"):
                    bad.append("置灰时示例指令应禁用（V6.7）")
                if not r.get("restored"):
                    bad.append("恢复可用态失败（会影响后续截图）")
                return bad

            if stub_ws:
                bad += await check_feed_feedback()
                await shot("8-反馈按钮")
                bad += await check_fb_stats()
                # 「反馈进化」是整行卡片、在首屏之下：不滚过去截出来的是页面顶部，
                # 看着"有图"其实验不到东西（截图必须能看见被验的那个东西本身）。
                await js("document.getElementById('agpFbKpis')"
                         "?.closest('.agp-card')?.scrollIntoView({block:'center'})")
                await shot("9-反馈进化区")
                bad += await check_agent_session()
                # 同样是首屏之下的整行卡片，先滚过去再截
                await js("document.getElementById('agtPrev')"
                         "?.closest('.agp-card')?.scrollIntoView({block:'center'})")
                await shot("10-对话调参")
                await js("document.getElementById('tabMain').click()")

            for name, tab_id in TABS:
                await js("document.getElementById('%s')?.click()" % tab_id)
                if tab_id == "tabAgent" and stub_ws:
                    await asyncio.sleep(0.4)
                    bad += await check_agent()
                    bad += await check_overflow()
                await shot(name)

            await js("document.getElementById('tabConfig')?.click()")
            await asyncio.sleep(0.3)
            await js(DEMO_JS)
            await shot("6-状态图标验证")

            await js("document.getElementById('tabMain')?.click()")
            await asyncio.sleep(0.3)
            await js("document.querySelector('.pipe-hint')?.scrollIntoView({block:'center'})")
            await shot("7-链路步骤条")

            if stub_ws:
                await js("document.getElementById('tabAgent')?.click()")
                await asyncio.sleep(0.3)
                v = json.loads(await js(V54_CHECK_JS))
                print("  试听接线（V5.4）:", json.dumps(v, ensure_ascii=False))
                if v.get("errs"):
                    bad.append("试听查验时页面报错：%s" % v["errs"][:2])
                if v.get("btnDisabled"):
                    bad.append("试听按钮在有条可试听回复时仍是禁用态（V5.4）")
                if v.get("previews") != 1:
                    bad.append("点试听没发出恰好一条 vc_preview（V5.4）")
                elif v.get("voiceId") != "zs-demo-voice":
                    bad.append("试听没带当前直播音色（V5.4）：%s" % v.get("voiceId"))
                if not v.get("textHead"):
                    bad.append("试听没带回复正文（V5.4）")

                if bad:
                    print("\n  !! 智能体页查验未通过：")
                    for b in bad:
                        print("     - %s" % b)
                    return 1
                print("\n  智能体页查验通过（V5.1 / V5.2 / V5.4 接线 / V5.6）")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        # 收掉自己那份 profile：进程彻底退出后再删，避免下次跑到残留的半死进程上
        shutil.rmtree(profile, ignore_errors=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="无头 Edge 截取控制台页签")
    ap.add_argument("--url", default=None, help="要截的地址；默认自起静态服务")
    ap.add_argument("--out", default=str(ROOT / "logs" / "_ui_shots"), help="输出目录")
    ap.add_argument("--port", type=int, default=PORT, help="自起静态服务端口")
    ap.add_argument("--view", default="%dx%d" % VIEW,
                    help="视口尺寸 WxH，如 1366x768（V5.6 要在这两档下都不溢出）")
    ap.add_argument("--stub-ws", action="store_true",
                    help="装 WebSocket 替身并喂夹具帧：让「智能体」页走真实渲染路径，"
                         "顺带查 V5.1 / V5.2 / V5.6（有 FAIL 退出码 1）")
    args = ap.parse_args()

    try:
        view = tuple(int(x) for x in str(args.view).lower().split("x", 1))
        assert len(view) == 2 and view[0] > 320 and view[1] > 240
    except Exception:
        print("--view 格式应为 WxH，如 1366x768", file=sys.stderr)
        return 2
    print("视口 %dx%d" % view)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    srv = None
    url = args.url
    if not url:
        srv = start_static_server(WEBUI, args.port)
        url = "http://127.0.0.1:%d/index.html" % args.port

    try:
        if not reachable(url):
            print("目标不可达：%s" % url, file=sys.stderr)
            return 2
        print("目标 %s" % url)
        rc = asyncio.run(capture(url, out, view, args.stub_ws))
    finally:
        if srv:
            srv.shutdown()
            srv.server_close()

    print("\n输出目录: %s" % out)
    return rc


if __name__ == "__main__":
    sys.exit(main())
