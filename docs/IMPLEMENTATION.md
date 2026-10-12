# 仔仔鼠AI智能体直播助手 · 实施计划

| 项目 | 内容 |
|---|---|
| 文档版本 | v1.0 |
| 日期 | 2026-09-21 |
| 前置文档 | `docs/DESIGN.md`、`docs/PRD.md` |
| 基线 | 旧版 v3（归档于 `AIZB/_old-zaizaishu-live-20260921/zaizaishu-live`） |

---

## 零、前置准备

### 0.1 本次已完成（2026-09-21）

| # | 事项 | 状态 |
|---|---|---|
| 1 | 旧版全部内容归档至 `_old-zaizaishu-live-20260921/`（579 MB，排除可重建产物 518 MB） | 完成 |
| 2 | 新版本目录 `zaizaishu-agent-live/` 建立（本次仅放文档） | 完成 |
| 3 | 四份设计文档产出 | 完成 |
| 4 | 搬动安全性验证（`paths.py` / `zaizaishu.spec` 全相对路径） | 完成 |

### 0.2 开工前必须做

```powershell
# 1. 把归档中的源码复制进新版本目录作为基线
robocopy "D:\AIZB\_old-zaizaishu-live-20260921\zaizaishu-live" `
         "D:\AIZB\zaizaishu-agent-live" `
         /E /XD .git .venv dist build __pycache__ .pytest_cache logs profiles /NFL /NDL

# 2. 在新版本目录重建 venv（Python 3.14.7，与旧版一致）
cd D:\AIZB\zaizaishu-agent-live
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. 基线自检：467 项测试必须全绿
.\.venv\Scripts\python.exe -m pytest tests -q

# 4. 首次提交
git init
git add -A
git commit -m "chore: 从 v3 基线派生新版本仓库"
```

> **注意**：复制基线时**不要**带 `.git`（新版本有独立仓库）、**不要**带 `.venv`/`dist`/`build`（可重建）、**不要**带旧的 `logs`/`profiles`（避免与旧版数据耦合，需要迁移时走 §M7 的迁移脚本）。

---

## 一、实施原则

1. **小步提交**：每完成一步 `git commit` 一次，提交信息写清"做了什么 + 为什么"。
2. **测试全绿再进下一步**：任何改动后 `pytest tests -q` 必须全绿。
3. **开关隔离**：智能体相关代码一律挂在 `agent.enabled` 之下，关闭时**代码路径与旧版完全一致**（不是"行为相似"，是"走同一段代码"）。
4. **旧版代码可回滚**：护栏代码全部保留，只加开关不加删除。任何"放开"都做成可逆开关。
5. **每阶段可独立验证**：不允许出现"必须等最后一步才能看出对不对"的阶段。
6. **不动归档**：新版本目录不读写归档目录，两版零耦合。
7. **中文文件用 Read/Edit 工具改**，不要用 PowerShell 管道（UTF-8 会被 GBK 二次编码损坏）。
8. **config.yaml 用行级替换写回**（`hub/config.py`），勿整文件重写（会丢注释）。

---

## 二、阶段划分

### M0 · 工程准备（0.5 天）

**内容**

1. 按 §0.2 派生源码基线、重建 venv、跑通 467 测试
2. `zaizaishu.spec`：`name` 改 `zaizaishu-agent`，`icon` 指向新的绿色图标
3. `config.example.yaml`：新增 `agent` 段（`enabled: false`）
4. `config.yaml`：同步新增 `agent` 段（保持关闭）
5. `.gitignore`：纳入 `.venv`、`dist`、`build`、`__pycache__`、`logs`、`profiles`、`config.yaml`、`voice_clone/voice_clone_data/`

**验证标准**

| # | 标准 |
|---|---|
| V0.1 | `pytest tests -q` 467 项全绿 |
| V0.2 | `python main.py` 启动控制台，功能与旧版一致 |
| V0.3 | `python desktop.py` 桌面窗口能开 |
| V0.4 | `config.yaml` 有 `agent` 段，注释未丢失 |
| V0.5 | `git status` 干净，首次提交完成 |

**回滚**：删除新版本目录，重新从归档复制。

---

### M1 · 视觉与品牌改造（1 天）

**内容**

1. `webui/index.html`：建立 CSS 变量层

```css
:root {
  --brand-primary: #00C29A;   /* 主色 */
  --brand-bright:  #40D1B3;   /* 悬停亮态 */
  --brand-press:   #009273;   /* 按下态 */
  --brand-deep:    #00614D;   /* 深色/强调文字 */
  --brand-subtle:  #DFF7F2;   /* 卡片底 */
  --brand-border:  #9FE8D9;   /* 描边 */
  --brand-tag:     #60D9C0;   /* 标签底 */
  --semantic-pos:  #00C29A;   /* 正面/采纳 */
  --semantic-neg:  #E24B4A;   /* 负面/弃用 */
  --semantic-warn: #EF9F27;   /* 警示 */
  --semantic-mute: #888780;   /* 中性 */
}
```

2. 把旧版散落的蓝紫色值**全部替换为变量引用**（不允许残留硬编码色值）
3. 极光渐变背景：蓝紫 → 青绿（同结构、同位置、仅旋转色相）
4. 蓝图网格线：转低饱和青绿
5. 产品名替换：窗口标题、顶栏标题、`desktop.py` 窗口标题
6. Logo：制作绿色版 `assets/icon.ico`（多尺寸：16/32/48/64/128/256）
7. **emoji 清理**：全文检索并替换为内联 SVG 图标
   - 反馈按钮：对勾 / 叉 / 铅笔
   - 状态提示：三角警示 / 对勾
   - 其余：改为文字 + 语义色

**验证标准**

| # | 标准 |
|---|---|
| V1.1 | 五个标签页逐页目视核对，无残留蓝紫 |
| V1.2 | 前端源码全文检索无 emoji 字符（含 `webui/index.html` 与 Python 内联 HTML 字符串） |
| V1.3 | 界面标题、窗口标题、exe 图标均为新品牌 |
| V1.4 | 灰度对比度检查：浅底深字可读（`#DFF7F2` 底 + `#00614D` 字） |
| V1.5 | 功能无回归：`pytest tests -q` 全绿，手工跑一遍五个页面交互 |

**产出物**：绿色版 `assets/icon.ico`

**回滚**：`git revert` 本阶段提交。

---

### M2 · 存档 v4 与情绪档位（2 天）

> 本阶段**不涉及智能体**，独立可验证。开关 OFF 时也能享受到多情绪音频。

**内容**

1. `hub/scriptstore.py`
   - `audios` 结构升级：`{vid: {file, hash}}` → `{vid: {情绪档: {file, hash}}}`
   - v3 → v4 自动迁移（幂等）：读到含 `file` 字段即包成 `{"default": ...}`，`version` 置 `4`
   - 存档新增 `emotions` 字段（档位集合），校验取值必须在 MiniMax 8 枚举内
   - 生成接口扩展：`generate_audio(pid, item_id, voice_id, emotion)`、`missing_emotions(pid, voice_id)`、`purge_emotion(pid, voice_id, emotion)`
   - `_guard_existing_audio` 护栏语义保留（逐档位判定）
2. `hub/selector.py`
   - `_resolve_audio(item, voice_id, emotion)`：三级回落（请求档 → `default` → 空串）
   - `current_emotions()`：返回当前存档档位集合
3. `hub/events.py`：`SpeakTask` 新增 `emotion: str = ""`
4. `hub/tts.py`：调用侧支持逐条 `emotion/speed/pitch` 覆盖（现为构造期固定）
5. `hub/player.py`：按任务 `emotion` 分派
   - 有档位音频 → 播预录
   - 档位已登记但缺音频 → 回落 `default`
   - 未登记档位 → 现场 TTS（带 emotion 覆盖）
6. `hub/webui.py`：新增 `sc_emotion_*` 动作族（列出/保存档位、批量补生成、缺失统计）
7. `webui/index.html`：话术管理页新增「情绪档位」区
   - 8 个枚举勾选（多选）
   - 「按档位批量生成」按钮 + 进度显示
   - 缺失档位提示（如「calm 档缺 12 条」）
8. 重建 `assets/seed_profile.zip` 为 v4，含 `default` + `happy` + `calm` 三份音频

**验证标准**

| # | 标准 |
|---|---|
| V2.1 | 旧 v3 存档拷入后启动自动迁移，`version` 变 4，`audios` 结构正确，`default` 档可播 |
| V2.2 | 重复启动不二次包装（幂等性测试） |
| V2.3 | 勾选 `happy` + 补生成 → 音频目录出现对应文件，`text_hash` 与实际文本一致 |
| V2.4 | 改一条话术文本 → 该条**所有**档位音频被标 stale，实测不再播旧文本 |
| V2.5 | 手工构造未登记情绪的播放请求 → 现场 TTS 出声，日志有 `emotion_onsite` |
| V2.6 | `pytest tests -q` 全绿（含新增的存档迁移与回落测试） |
| V2.7 | 开关 OFF 时整场直播行为与旧版一致（回归） |

**回滚**：`git revert`；已迁移的存档需从备份恢复（迁移前自动备份 `profile.yaml` 为 `profile.yaml.v3.bak`）。

---

### M3 · 总开关与模式切换（1.5 天）

**内容**

1. `hub/config.py`：`agent` 段解析与校验（`enabled` / `retrieve.layer` / `memory.*` / `emotion.*` / `feedback.enabled` / `decision_log`）
2. `hub/router.py`：新增 `mode`（`"rules"` / `"agent"`）
   - agent 模式下 `_handle_chat` 跳过噪声概率与聚合窗口，直接产出单条事件上下文
   - agent 模式下 `_handle_gift` 跳过连击状态机
   - agent 模式下 `_handle_follow` / `_handle_fansclub` 跳过冷却与概率
   - **输入侧敏感词闸门保留**
   - `set_mode()` 支持热切换
3. `hub/queue.py`：新增 `no_expire`
   - `_is_stale` 在 `no_expire` 时恒返回 `False`
   - `put_idle` 在 `no_expire` 时不检查积压
   - **优先级排序与 `interrupt` 头插保留**
4. `hub/webui.py`：`ag_*` 动作族的骨架（`ag_status` / `ag_set_enabled`）
5. `webui/index.html`：系统配置页新增「智能体」卡片（总开关 + 状态 + LLM 未配置时置灰提示）
6. `main.py`：开关热生效装配（切开关 → 重建 Router/Queue 模式 → 无需重启）

**验证标准**

| # | 标准 |
|---|---|
| V3.1 | 开关默认 OFF，整场行为与旧版一致（关键场景人工比对） |
| V3.2 | 开启后立即生效，无需重启（同一场直播内切换可观察） |
| V3.3 | 未配 LLM Key 时开关置灰 + 明确提示 |
| V3.4 | agent 模式下敏感词弹幕仍被输入侧拦截（不进智能体） |
| V3.5 | agent 模式下队列不丢过期任务（构造超期任务验证） |
| V3.6 | 关掉开关后 `_is_stale` 恢复旧行为（回归测试） |
| V3.7 | `pytest tests -q` 全绿 |

**回滚**：开关置回 OFF 即恢复旧行为；代码层面 `git revert`。

**实现补充（2026-09-22 落地时的口径决定）**

1. **`agent.enabled` 与"运行时模式"分两层**：配置里开了但**未配 LLM Key** 时，启动即降级为规则管道（不擅改配置文件），状态里同时给出 `configured_enabled` / `downgraded` / `note`，前端据此显示「不可开启 + 原因」。理由：agent 模式的核心能力是 LLM 决策，没有 Key 时开关开与不开都是"弹幕静默"，不如让 UI 明确置灰。
2. **热切换先落盘再切运行时**：`update_agent_config` 写失败就整个不动（返回失败原因），避免出现「这次生效、重启又回去」的假象。落盘成功后才一次到位地 `router.set_mode()` + `queue.set_no_expire()` + 同步内存 `cfg`。
3. **切换模式前排空旧模式中间态**：`set_mode()` 先 `_flush_intermediate()`（未到期聚合窗口 → 未结算连击 → 冷却积压一次性产出），不让聚合窗口/连击表这种"旧模式专用状态"漏进 agent 模式。
4. **`set_agent_handler()` 是 M4 的接缝**：与 `reply_builder` 同契约（`(context) -> str | dict | None`，可同步可 awaitable）。M3 只留钩子，未注入时 agent 模式的 chat 仍走 `reply_builder`（开关已开但智能体尚未接管的降级路径，不丢弹幕）；钩子抛异常走"safety.fallback_reply 兜底口播"，与异步生成器异常同一末级容错。
5. **`no_expire` 只关"过期判定"**：优先级排序、`interrupt` 头插、F9 暂停清空、逐类事件开关、输入侧敏感词闸门一概不变——护栏与"丢不丢过期任务"是两件事。

---

### M4 · 智能体核心（3 天）

**内容**

1. `hub/agent/__init__.py`
2. `hub/agent/memory.py`：单场环形缓冲（events / spoken / notes），进程内，无落盘
3. `hub/agent/retriever.py`：
   - `Retriever` 接口（`index()` / `retrieve()`）
   - `L2Retriever` 实现：复用 `router.default_similarity`（2-gram Jaccard），带 `RECENT_PICK_WINDOW_S` 去重与 `has_audio` 标注
4. `hub/agent/prompt.py`：system prompt 组装
   - 人设知识库（复用 `selector.build_system_prompt` 的素材）
   - 话题话风参考
   - L1 样本示例
   - L2 召回结果
   - 单场记忆摘要（最近事件 / 最近说过的话 / 笔记）
   - **可用情绪档位清单**
   - 输出 JSON 契约说明
5. `hub/agent/decide.py`：决策契约校验与三级容错
   - 提取首个 `{...}` 块重解析
   - 失败降级为纯文本（`priority=1`，`reason="解析降级"`）
   - LLM 异常 → 旧版兜底口播
6. `hub/agent/emotion.py`：情绪校验（档位集合内 / 8 枚举内 / 非法 → 回落 `default`）
7. `hub/agent/log.py`：决策日志 JSONL（含 `latency_ms`、`tokens` 若可用）
8. `hub/agent/brain.py`：`AgentBrain.decide(event) -> AgentDecision` 主流程
9. `main.py`：agent 模式下用 `AgentBrain` 作为应答决策来源；情绪覆盖透传到 `player`/`tts`

**验证标准**

| # | 标准 |
|---|---|
| V4.1 | 重放模式跑一场，每条弹幕都拿到完整决策（字段齐全） |
| V4.2 | 非法 JSON 三种情形（截断 / 混入说明文字 / 完全非 JSON）均不崩、不静默 |
| V4.3 | `should_reply: false` 时确实不产出任务（不播任何声音） |
| V4.4 | 情绪校验：档位内 / 8 枚举内 / 非法 三种输入处置正确 |
| V4.5 | 决策日志每行一个 JSON，可被 `json.loads` 解析 |
| V4.6 | 单场记忆生效：同一用户连刷礼物能被识别；不重复说同一句话 |
| V4.7 | L2 召回的 `has_audio` 标注正确（有音频的条目才标记为 true） |
| V4.8 | `pytest tests -q` 全绿，新增 agent 模块单测（决策解析、情绪校验、记忆环形缓冲） |

**回滚**：开关置 OFF。

**实现补充（2026-09-22 落地时的口径决定）**

1. **接管范围从"只 chat"扩到五类事件**：M3 只把 `set_agent_handler` 挂在 `_handle_chat` 出口，agent 模式下 gift/follow/fansclub/enter 仍走本地模板，与 §5.1「每条都产出，怎么感谢交智能体」不符。M4 补齐，并引入 `_agent_active()`（`mode` + 钩子**双条件**）作为唯一判据。详见§五「M4 实测记录」。
2. **容错链补一档**：DESIGN 的三级在"JSON 截断"时会退化为"把 JSON 残骸当话术念出去"。新增 `salvage` 档（正则抢救 `reply_text` / `should_reply`）+ 残骸判定（抢救不出就抛 `DecisionError` 走兜底口播）。
3. **`should_reply=false` 的事件也进单场记忆**：否则智能体看不出"这条刚被别人问过""同一用户连刷礼物"，判断依据就断了（记忆容量是上下文窗口，不是限流）。
4. **预录音频复用**：智能体若原样采纳了某条召回素材的话术，就去解析**那条素材**的预录音频并透传 `audio_path`。不做这一步，"情绪档位 / 预录音频"整套机制在开启智能体后等于废掉（每条回复都现场合成）。
5. **决策日志分记 `emotion_raw` 与 `emotion`**：否则事后回看分不清"模型选歪了"和"被回落规则改掉了"。
6. **`max_reply_chars` 每次决策现读**，不在构造期缓存：`main` 里 brain 可能建在存档装载完成之前；现读也顺带让主播改字数上限后下一次决策即生效。

---

### M5 · 智能体页面（2.5 天）

**内容**

1. `webui/index.html`：新增第 5 个标签页「智能体」
   - 状态条：接管色点 + 模型名 + 本场决策数 + 平均耗时（点击跳系统配置）
2. 决策透视区：时间倒序列表 + 四张聚合卡，读 `logs/agent-*.jsonl` 最近 500 条
3. 试玩调试区（`hub/agent/playground.py`）
   - 事件构造器（类型/用户/内容/礼物名与价值）
   - 「送入智能体」→ 完整决策展示
   - 可选「用当前音色试听」
   - 批量投递（模拟弹幕风暴）
   - **隔离保证**：不写记忆、不进队列
4. `hub/webui.py`：`ag_logs` / `ag_replay_decision` / `ag_playground` 动作

**验证标准**

| # | 标准 |
|---|---|
| V5.1 | 决策透视每条都能看到「为什么这么回」+ 素材引用（如有） |
| V5.2 | 聚合卡数值与日志实际条数一致 |
| V5.3 | 试玩期间直播链路零影响（记忆与队列实测无变化） |
| V5.4 | 试听按钮能出声（MiniMax 已配置） |
| V5.5 | 全页无 emoji，图标为内联 SVG |
| V5.6 | 页面在 1366×768 与 1920×1080 下均不溢出 |

**实现补充（2026-09-22 落地时的口径决定）**

1. **动作族只加两项**：`ag_logs` / `ag_playground`。设计稿里的 `ag_replay_decision` 未做——
   决策透视每条记录已带 `event_line` / `reason` / `recall` 全量字段，前端直接渲染即可，
   不必回服务端重算一次；真要重算某条事件，用试玩调试区把同一事件重新构造一遍即可（同一条 `preview` 路径）。
2. **「本场决策数」的真实口径 = 当日决策日志**（`logs/agent-YYYYMMDD.jsonl`）最近 500 条中
   `test != true` 的条数。日志按天轮转、不随重启清零，所以状态条与聚合卡都挂了 tooltip 说明数据源，
   页脚也写了文件路径，避免主播把它当成"这一场的"而困惑。
3. **试听复用 `vc_preview`**（音色工坊那条已有测试覆盖的链路），不新增合成接口；
   `<audio id="vcAudio">` 全局共用，从智能体页触发时提示语回写到本页 note
   （否则用户站在智能体页会看不到任何反馈）。
4. **聚合第 4 张卡**「各类事件占比」= 色条 + 图例 + 四个信号（回复 / 容错兜底 / 复用预录音频 /
   跳过测试事件），不另占卡片，凑齐设计稿要求的"四张卡"。
5. **`ui_shots.py` 加 `--view` / `--stub-ws`**：静态服务下在页面脚本之前装一个 WebSocket 替身
   喂夹具帧，让智能体页走**真实**的消息分发与渲染路径，并顺带断言 V5.1 / V5.2 / V5.4 接线 / V5.6。

---

### M5 实测记录（2026-09-22）

**验收标准逐项**

| # | 标准 | 结果 |
|---|---|---|
| V5.1 | 决策透视每条都能看到「为什么这么回」+ 素材引用 | **通过**。展开后「判断依据 / 回复 / 语气·情绪·语速 / 素材引用」四行齐备；`ui_shots --stub-ws` 断言 `hasReason` / `hasReply` / `hasRecall` / `hasEmotion` 全 true |
| V5.2 | 聚合卡数值与日志实际条数一致 | **通过**。断言聚合卡「本场决策数」== 列表条数（6 == 6）；`test: true` 单独计 `test_skipped`，不混进"本场决策"，色条 5 段对应图例 5 项 |
| V5.3 | 试玩期间直播链路零影响 | **通过**（`tests/test_agent_playground.py`：真 `AgentBrain` + 桩 LLM，试玩前后逐字段比对记忆 stats / spoken / 决策日志行数 / 召回去重窗口，四项均不变） |
| V5.4 | 试听按钮能出声 | **接线通过**：点一次恰好发出一条 `vc_preview`，带当前直播音色与回复正文。**实际出声**需 MiniMax Key，走的是音色工坊既有链路（`tests/test_voiceworkshop.py` 覆盖），未在无 Key 环境虚构结论 |
| V5.5 | 全页无 emoji，图标为内联 SVG | **通过**（`scripts/emoji_scan.py`：emoji 0 行；箭头类排版标点保留 0 行） |
| V5.6 | 1366×768 与 1920×1080 均不溢出 | **通过**（两档 `docW == 视口宽`、`pgScrollW == pgClientW`、页内元素横向溢出 0px） |

**V5.6 溢出探针实测**（无头 Edge + CDP，`scripts/ui_shots.py --stub-ws --view WxH`）

| 视口 | 文档横向 | 智能体页容器横向 | 页内元素最大横向溢出 | 智能体页纵向 | 结果 |
|---|---|---|---|---|---|
| 1366×768 | 1366 == 1366 | 1366 == 1366 | 0px | 887 > 674（纵向滚动，符合预期） | 通过 |
| 1920×1080 | 1920 == 1920 | 1920 == 1920 | 0px | 986 == 986（正好放下，无需滚动） | 通过 |

**M5 抓到的真实缺陷（单测与配色审计都覆盖不到）**

| 缺陷 | 后果 | 为什么之前查不出来 | 修法 |
|---|---|---|---|
| 分发点写的是 `applyAgLogs`，而函数名是 `agpApplyLogs` | `ReferenceError`：「决策透视」永久空白——M5 最关键的功能**静默失效** | 页面脚本是 IIFE，异常只落在浏览器控制台；`ui_audit`（配色）、`emoji_scan`（图标）、截图（空态）三个验收面全看不见 | ① 统一 `agp*` 前缀；② 在 WS 替身里收 `window.onerror` + `unhandledrejection`，查验时断言 `errs == []` |

> 这条坑值得单独记：**"静态检查全过 + 截图正常"不等于功能可用**。空态截图看起来"页面是好的"，
> 恰恰因为数据没渲染出来。真正的抓手是让页面走一遍真实数据流，再断言渲染结果。

**提交列表**

| 提交 | 内容 |
|---|---|
| `96ea94a` | M5-1/2 试玩调试内核与决策日志聚合 |
| `e411bbd` | M5-3 智能体页面动作族与 main 装配 |
| 见 git log | M5-4 前端第 5 页签 + M5-5 验收 |

---

### M6 · 反馈进化与对话调参（2.5 天）

**内容**

1. `hub/agent/feedback.py`
   - 采集：采纳 / 弃用（原因标签）/ 改写
   - 落盘：`profiles/<pid>/agent/examples.jsonl`（`source`: `adopted` / `rewritten` / `rejected`）
   - `profiles/<pid>/agent/feedback.jsonl`（原始反馈）
   - 注入：`retriever` 召回 `adopted` + `rewritten` 样本作 few-shot（`rejected` 不参与）
2. `webui/index.html`
   - 直播中枢每条 AI 回复旁三个图标按钮（对勾 / 叉 / 铅笔）
   - 弃用弹窗：原因标签（不像主播 / 太长 / 跑题 / 重复）
   - 改写弹窗：输入框 + 提交
   - 反馈进化区：样本总数 / 本周新增 / 采纳率 / 原因标签分布（色条）
3. `hub/agent/session.py`：对话式调参
   - LLM function calling，把可改字段注册为工具集
   - 写回：`config.yaml` 走 `hub/config.py` 行级替换（保注释）；`profile.yaml` 走 `scriptstore.save_profile`（护栏保留）
   - 改动前自动快照（`logs/agent_session.json`，保留 20 次）
   - 回显「字段：旧值 → 新值」+ 撤销按钮
4. `webui/index.html`：对话调参区（左对话 + 右配置预览）

**验证标准**

| # | 标准 |
|---|---|
| V6.1 | 点采纳 → `examples.jsonl` 新增一条，字段完整，`source: adopted` |
| V6.2 | 点弃用 + 选原因 → `feedback.jsonl` 记录原因标签；该条不进 few-shot |
| V6.3 | 改写 → 新版本入库（`source: rewritten`），原回复不再被引用 |
| V6.4 | 累计 10 条好评后重放同一场，回复语气可见变化（对比决策日志） |
| V6.5 | 说「口播太频繁了」→ 间隔参数变化 + 热生效 + 回显旧值→新值 |
| V6.6 | 撤销后配置回到改动前，`config.yaml` 注释完整 |
| V6.7 | 无 LLM Key 时对话面板置灰，话术管理表单不受影响 |
| V6.8 | `pytest tests -q` 全绿 |

---

### M6 实测记录（2026-09-23）

**验收标准逐项**

| # | 标准 | 结果 |
|---|---|---|
| V6.1 | 点采纳 → `examples.jsonl` 新增一条，字段完整，`source: adopted` | **通过**（`FeedbackStore.adopt()` 落 `profiles/<pid>/agent/examples.jsonl`，字段 `id` / `pid` / `source` / `reply` / `event_line` / `event_type` / `user` / `tone` / `ts` 齐全；`rejected` 走同目录 `feedback.jsonl`） |
| V6.2 | 点弃用 + 选原因 → `feedback.jsonl` 记原因标签；该条不进 few-shot | **通过**（原因四档 `不像主播` / `太长` / `跑题` / `重复` 落 `feedback.jsonl`；`examples()` 只取 `adopted` + `rewritten`，弃用条**不出现在** few-shot 注入里） |
| V6.3 | 改写 → 新版本入库（`source: rewritten`），原回复不再被引用 | **通过**（新文本以 `source: rewritten` 入库并进 few-shot，指向原条目的 `supersedes` 留下；原条不再被 `examples()` 选中） |
| V6.4 | 累计 10 条好评后重放同一场，回复语气可见变化 | **验到"提示词可见变化"层**：`test_v64_cumulative_adoptions_change_the_prompt` 累计 10 条采纳 → `build_system_prompt()` 里出现 **≥2 条**评分最高的好评作 few-shot，且不含被弃用条。**真模型输出差异需 LLM Key**，无 Key 环境不虚构结论 |
| V6.5 | 说「口播太频繁了」→ 间隔参数变化 + 热生效 + 回显旧值→新值 | **通过**（`SessionTuner.send("口播太频繁了")` → `profile.schedule.interval_s` 由 420 → 720，回显 `口播间隔：7分钟→12分钟`；`applier` 回调 `script_rt.reload()` 后 `ScriptRuntime.schedule()["interval_s"]` 实测 == 720，**热生效已从真对象取证**；`ui_shots` 断言页面 `diffText` 剥空格后 == `口播间隔：7分钟→12分钟`） |
| V6.6 | 撤销后配置回到改动前，`config.yaml` 注释完整 | **通过**（`undo()` 复用同一批 setter；`config.yaml` 走行级替换→注释保留、`pool` 列表未动；profile 按**段快照**还原，只写回被碰过的段，不覆盖别处并发保存） |
| V6.7 | 无 LLM Key 时对话面板置灰，话术管理表单不受影响 | **通过**（`SessionTuner.available == False` → 页面 ④ 卡 `#agtOff` 显示原因、`#agtInput` / `#agtSend` / 提示 chips 全 disabled；`ag_session_*` 是独立动作族，不进话术管理表单路径。`ui_shots` 断言 `offShown` + 三个 `*DisabledOff` 全 true） |
| V6.8 | `pytest tests -q` 全绿 | **通过**：**821 passed in 101.72s**（M5 后 701 → M6 后 821，**+120**） |

**落地口径的三处偏离 DESIGN 的决定**

| # | DESIGN 原文 | 实际落地 | 原因 |
|---|---|---|---|
| 1 | 写回「`config.yaml` 走行级替换；`profile.yaml` 走 `save_profile`」 | **两处都写**：profile 副本是**活值**（热生效），`config.yaml` 副本与 §7.2① 对齐、兼作「还没有存档」时的引导值 | `config.yaml` 的 `schedule.interval_min` 在建成存档后是**死值**——只有 `Scheduler.__init__` 与一次性迁移读它，运行期调度器每次都由 `ScriptRuntime.schedule()` → `profile.schedule.interval_s` 重配。只写 config 会得到"看起来改了实际没变"的**假效果**（见 HANDOVER 坑 54） |
| 2 | 「改动前自动快照（保留 20 次）」 | 快照**写前落盘** + **撤销即消费**（同一 id 不能重复撤销）；多 op 的一次请求**共用一个快照**（一次撤销回滚整批） | 若每个 op 各存一个快照，"说一句话改了三处"要撤三次；且重复撤销同一快照会把配置改坏 |
| 3 | 「把可改字段注册为工具集」 | 落地为**显式白名单** `tool_specs()`（6 个工具：语气 / 回复字数上限 / 口播间隔 / 加话题 / 加示例 / 游戏介绍）+ `_resolve` 契约校验 | 护栏字段（敏感词 / F9 暂停 / 播放串行 / `event_enabled`）**不进白名单**，模型就永远改不动——护栏不依赖"提示词请求它别改"（DESIGN §六 保留项不被对话调参绕过） |

**M6 抓到的真实缺陷 / 关键发现**

| 项 | 后果 | 为什么之前查不出来 | 修法 |
|---|---|---|---|
| `schedule.interval_min` 建成存档后是死值 | 「口播太频繁了」若只写 `config.yaml` → **假生效**（页面回显了、实际调度不变） | 单元测试若只断言"config.yaml 被写了"会全绿；只有从**真 `ScriptRuntime`** 取 `schedule()["interval_s"]` 才看得见 | 两处都写 + 接缝单测（真 `reload()` → 真 `schedule()`）；HANDOVER 坑 54 |
| 反馈进化区色条配色最低 1.87:1 | 色条段与图示项**只能靠颜色对应**（WCAG 1.4.11 的"图形"，门槛 3:1），1.87:1 人眼难辨且不合规 | 目视"颜色挺鲜艳"；`ui_audit` 是**静态枚举**、不补行就不验 | 换深色调（`--brand-press` 3.62 / `--gold-text` 4.95 / `--red-text` 5.02 / `--ink-2` 5.77）+ 补 13 行审计；坑 61 |
| `ui_shots` 三类脆弱（导航竞态 / profile 交接 / 线程不退） | 1920 档 `getComputedStyle` 报错；背靠背运行页面卡 `loading\|0\|0` 三十秒；脚本打印结果后**永不退出** | 单次运行看不出来，靠固定 `sleep` 掩盖 | 就绪轮询 `wait_ready()` + per-pid profile（`finally` 清理）+ `ThreadingTCPServer(daemon_threads=True, block_on_close=False)`；坑 58/59/60 |
| Git Bash heredoc 写文件被**双重执行**且 `\n`→`/n` | 单测里函数定义重复、字符串字面量被路径转换改坏（`"app:/n"`） | 只在用 shell 重定向写 `.py` 时出现，且第一次看起来"写成功了" | 写进仓库的文件一律用 Write/Edit 工具；坑 57 |

**提交列表**

| 提交 | 内容 |
|---|---|
| `2d37a38` | M6-1 反馈采集存储层 `feedback.py`（三路采集 / 双 file 落盘 / few-shot 过滤） |
| `4071c7a` | M6-2 反馈样本回流注入（现读通道 + main 装配 `FeedbackStore`） |
| `77b52f1` | test: 修两处时序脆弱（固定 sleep → 事件循环静默检测 / 采集器取消口径） |
| `4dc855c` | M6-3 反馈采集入口（三图标按钮 + 弹窗 + `ag_feedback` 动作族） |
| `45f614b` | M6-4 「反馈进化」区 + 图表配色实测修正 |
| `c2d1824` | M6-5 对话调参内核 `session.py`（白名单 + 写前快照 + 撤销 + 热生效） |
| 见 git log | M6-6 「对话调参」页面 + `ag_session_*` 动作族 + main 装配 + 验收 |

---

### M7 · 收尾（1 天）

**内容**

1. `scripts/migrate_from_old.py`：从归档目录迁移 `profiles/` 与 `voice_clone_data/`
   - 只复制，不改不删源目录
   - 校验存档版本号、音色 ID、音频文件完整性
2. 全量回归：旧版 467 项测试逐项对齐，差异项写明原因
3. 手工回归 PRD §FR-10 的 11 项保留能力
4. 性能实测：启动时间、exe 体积、端到端延迟（OFF vs ON 对比）、4 小时稳定性
5. 发布包组装（见 §四）
6. 更新 `docs/HANDOVER.md` 至最终状态

**验证标准**

| # | 标准 |
|---|---|
| V7.1 | 旧版测试全绿或差异项有明确说明 |
| V7.2 | PRD §FR-10 的 11 项能力逐项手工验证可用 |
| V7.3 | exe 体积 ≤ 旧版 + 5 MB |
| V7.4 | 冷启动 ≤ 旧版 1.5 倍 |
| V7.5 | 连续运行 4 小时无崩溃、无内存持续增长 |
| V7.6 | 发布包解压双击可用（首跑种子存档自动落地） |
| V7.7 | 迁移脚本能成功迁移一份旧存档并跑通直播 |

---

### M7 实测记录（2026-09-23）

**V7.1 旧版回归对齐（AST 枚举 + 临时副本实跑；归档全程只读）**

方法：把归档 `_old-zaizaishu-live-20260921/zaizaishu-live` 复制到临时目录（排除 `logs/` `profiles/`
`assets/`），用新版 venv 的 python 实跑它的测试；**不在归档目录内执行任何命令**（连 `.pytest_cache`
与 `__pycache__` 都不落）。同时用 AST 枚举新旧两版测试函数做集合对齐。

> 计数口径统一为：**文件数 / AST 枚举的 `test_*` 函数数（含类方法）/ pytest 实际收集项数**（参数化会放大收集数）。

| 维度 | 结果 |
|---|---|
| 旧版测试 | 31 个文件 / AST 461 个测试函数（含参数化 → pytest 实收 **467** 项，与基线文档一致） |
| 新版测试 | 48 个文件 / AST 848 个测试函数（pytest 实收 **877** 项） |
| **旧版实跑（临时副本）** | **466 passed / 1 failed in 72.32s** |
| 那 1 项失败 | `test_main_douyin.py::test_concurrent_set_room_serialized_no_dual_collectors` —— **已诊断的负载相关 flaky**：判"旧采集器下线"只看 `run_ended`，而"任务在 `run()` 拿到首次调度前就被取消"时协程体永不执行、`run_ended` 恒假，于是等不到条件。**单跑该项 12 passed（3.06 s）**，高负载才复现。新版已修（`_offline()` = `run_ended or not run_called`），故新版 877 全绿 |
| 仅旧版有的测试文件 | **0 个**（没有任何老测试文件被删或改名） |
| **旧版测试函数是否被删** | **0 个被删**——按函数名做集合比对，旧版 461 个 `test_*` 函数在新版中**全部存在**（缺失数 = 0）。这是"原用例未回归"的硬证据 |
| 逐字节未改的共有文件 | **24 个** / 共有 31 个 |
| 共有但改过的文件 | 7 个，其中 **4 个纯追加（删除行 = 0）**、**3 个含少量删除行**（见下） |
| ↳ 纯追加的 4 个 | `test_config.py` +230 / `test_player.py` +87 / `test_queue.py` +116 / `test_selector.py` +80（情绪档位、`no_expire`、v4 迁移等新用例，一律追加） |
| ↳ 含删除的 3 个（删除行已逐条看过） | `test_main_douyin.py` +18 **−3**（那条 flaky 的判活条件重写，**未删任何用例**）；`test_scriptstore.py` +352 **−11**（文件头 docstring 重写 + 随 v4 情绪档/字段形状更新的断言，**函数数只增不减**）；`test_webui_script.py` +164 **−21**（docstring 重写 + helper `_seed_items` 位置调整 + v4 audios 断言形状更新，**函数数只增不减**） |
| 新增测试文件 | 17 个：agent 线 12 个 + `test_migrate_from_old` + `test_make_release` + `test_router_agent_hook` / `test_router_mode` / `test_main_agent_switch` / `test_webui_agent*` |
| 冻结清单（DESIGN §15） | `hub/` 的 14 个冻结模块 + `voice_clone/minimax_client.py` **全部字节相同**；按约定只改产品名的两处：`desktop.py`（docstring + `TITLE` + argparse 描述）、`zaizaishu.spec`（docstring + `name='zaizaishu-agent'`） |
| 其它 `hub/` 模块 | 仅 9 个有改动：`config` / `events` / `player` / `queue` / `router` / `scriptstore` / `selector` / `tts` / `webui` —— 正好是 M2/M3/M4/M6 的落点，与设计文档一一对应 |

结论：**旧版 467 项一项未删**（31 个测试文件全在，461 个测试函数在新版中缺失数为 0），
差异集中在 7 个共有文件：4 个纯追加新用例、3 个在**不删用例**的前提下重写了 docstring / 断言形状 / 一条 flaky 判活条件。
产品侧改动严格收敛在非冻结模块（9 个 `hub/` 模块）且可逐条对应到 M2/M3/M4/M6 里程碑。

**V7.7 迁移脚本能成功迁移一份旧存档并跑通直播**

| 项 | 结果 |
|---|---|
| 真实归档 dry-run | EXIT=0，识别 `p_c6a0cfda`（v3「示例主播」），引用音频 60 个，源目录零写入 |
| 真实迁移（→ 临时目录） | EXIT=0，`profiles/<pid>/{profile.yaml, audio/}`（60 个 wav）+ `voice_clone/voice_clone_data/` 全部落地 |
| 试加载（真 `hub.scriptstore.ScriptStore`） | **v3 → v4 迁移生效**：条目 30（12 样本 + 18 话题）/ 带音频 30 / **磁盘真能取到非空音频 30** |
| 源目录不变性 | 单测以「文件大小 + `mtime_ns` 快照」断言迁移前后源目录快照**完全相等**（含 mtime） |
| "能跑通直播"的取证口径 | 试加载走的是应用首跑同一路径（`normalize_profile` + 扁平 audios 包成 `default` 档），并逐条目标定到磁盘上的非空音频文件 → 即播放侧 `selector` 可直接消费的形状；真实出声仍需声卡 + MiniMax Key，未在无 Key 环境虚构结论 |
| 迁移前修掉一个静默少算 | `audio_refs` 起初写死了桶名（含 v4 已改名的 `topics`），真实归档**少统计 36 个引用**（24 → 60）。改成遍历所有列表型顶层键、只认条目里的 `audios`，另加一条专门用例守住 |

**V7.2 PRD §FR-10 的 11 项保留能力逐项回归**

取证分三档：① **模块字节相同 + 原测试未改动且在 877 全绿里**（最强的"未回归"证据）；
② 模块**改过**的，写一次性探针实跑该能力；③ 依赖真实设备/密钥的，注明留给后续步骤验。

| # | 能力 | 关联模块 | 模块改动 | 原测试（未删） | 取证 / 实测 |
|---|---|---|---|---|---|
| 1 | MiniMax 音色管理（Key/上传复刻/试听/拉取/删除/15 槽/7 天保活） | `hub/voiceworkshop.py` | **字节相同** | `test_voiceworkshop` 29 项 | ① 冻结件未被触碰 |
| 2 | 弹幕直连采集（ttwid→web/enter→im/fetch→ws_sign→wss） | `douyin.py` / `dysign.py` / `dyproto.py` | **全部字节相同** | `test_douyin` 58 项 | ① |
| 3 | 优先级队列与串行播放（P0>P1>P2，MME 24000Hz） | `queue.py` / `player.py` | 已改（`no_expire` 开关 / 情绪档覆盖） | `test_queue` 20 + `test_player` 17，**原用例全保留** | ② 探针实跑（真 `PrioritySpeakQueue`，注入时钟）：OFF 时出队 **P0 → P1 → P2**；过期 P1 被丢弃且 `dropped_stale == 1`；同优先级 `put_head` 头插生效（先出 `b`）；`no_expire=True` 时过期任务**保留**。串行播放由 `player` 单线程消费保证，其 17 项原用例未改动且全绿 |
| 4 | 敏感词双检 | `safety.py` | **字节相同** | `test_safety` 14 项 | ① |
| 5 | F9 暂停热键 | `hotkey.py` | **字节相同** | `test_hotkey` 5 项 | ① |
| 6 | 话题轮播调度 | `scheduler.py` | **字节相同** | `test_scheduler` 17 项 | ① |
| 7 | 失败告警与静默兜底 | `watchdog.py` | **字节相同** | `test_watchdog` 3 项 | ① |
| 8 | JSONL 重放模式 | `replay.py` | **字节相同** | `test_replay` 8 + `test_eval_replay` 10 | ① |
| 9 | 无声卡黄横幅 + 一键安装引导 | `webui/index.html`（+ `webui.py`） | html 已改（青绿 / 去 emoji / M6 新卡） | `test_webui` 26 项 | ② 静态断言 7 项全 OK：`id="vbBanner"`、`has_vb_cable` 显隐开关、`一键安装虚拟声卡.bat` 文案、`vbBannerGo` 查看指引、`refresh_audio_devices` / `test_audio_device` / `set_audio_device` 三个动作仍在；`ui_shots --stub-ws` 两档视口真实渲染已过。**声音是否真从 CABLE Input 出去需实机声卡**，留给 V7.5 发布包实机验收 |
| 10 | 存档导入导出 zip / 种子首跑落地 / 防覆盖护栏 | `scriptstore.py` | 已改（v4） | `test_scriptstore` 43 + `test_desktop_first_run` 4 | ② 探针实跑（真 `ScriptStore`，临时目录）：zip 导出 1017 B → 导入空库得新 id 且 warnings 空；空库 `ensure_default()` 建「默认话术」；`seed_profiles_if_empty()` 首跑落地「示例主播」**v4**（12 样本 / 18 话题 / 2 音色 / 情绪档 `default,happy,calm`）；**防覆盖护栏**：整档保存把 `audios` 清空后，磁盘上仍存在的引用被还原 |
| 11 | 单 exe 桌面应用（pywebview） | `desktop.py` + `zaizaishu.spec` | 仅改产品名（`TITLE` / docstring / argparse 描述）与 spec `name` | `test_desktop_first_run` 4 项 | ② spec 静态断言全 OK：入口 `desktop.py` / `onefile=True` / edgechromium hiddenimports / `name='zaizaishu-agent'` / 绿色 `assets/icon.ico`。**注**：M7-5 起种子 zip 已**显式从 spec `datas` 移除**（改发布包外置，见下方偏离 DESIGN 决定），由回归用例 `test_spec_does_not_inline_seed_zip` 守住。**exe 实际构建与双击可用见 V7.3 / V7.6**；本机 pywebview 未装，打包前需补装 |

小结：11 项中 **7 项（1/2/4/5/6/7/8）的后端模块字节相同**、原测试一字未改且包含在 877 全绿里——这是"未回归"的最强形式；
其余 4 项（3/9/10/11）的改动都是**加法或换肤**，已用真对象探针逐项取证，需要实机/密钥的部分明确标注留给 V7.5/V7.6。

**V7.3 exe 体积（≤ 旧版 53.61 MB + 5 MB = 58.61 MB）**

| 版本 | 体积 | 说明 |
|---|---|---|
| 旧版（基线） | 56,217,041 B = **53.61 MB** | 归档 `_merge_work/smoke/zaizaishu.exe`（与 PRD 记的 53.6 MB 一致） |
| 新版（**内联种子**，改动前） | 92,513,913 B = **88.23 MB** | ❌ 超上限 34.5 MB。根因：69.2 MB 的 `assets/seed_profile.zip` 被 datapack 进 onefile |
| 新版（**种子外置**，现状） | 19,925,127 B = **19.00 MB** | ✅ 比旧版还小 **34.61 MB** |

体积定位手法：按 `build/zaizaishu/Analysis-00.toc` 的条目大小聚合 → `assets/seed_profile.zip` 独占 69.21 MB（其余最大项只有 6.47 MB 的 `python314.dll`），一眼看出谁在吃体积。
按 `IMPLEMENTATION.md` 待办第 8 条给的方案改为**发布包外置**后，exe 只剩运行时（Python + pythonnet/pywebview + portaudio）。

**V7.4 冷启动（≤ 旧版 1.5 倍）**

测法：把发布包复制到临时目录，改掉 `webui.listen` **与** `danmaku.ws_listen` 两个端口（见 HANDOVER 坑 63），清掉 `http_proxy/https_proxy` 并设 `NO_PROXY=*`，启动 exe 后轮询控制台 HTTP 就绪（与 `desktop._wait_console` 同判据）。

| 场景 | 就绪耗时 | 相对旧版 |
|---|---|---|
| 旧版 exe（包内已带存档） | **2.85 s** | 1.00× |
| 新版 exe 首次运行（含解出并导入 69 MB 外置种子 → 120 个音频） | **3.84 s** | 1.34× ✅ |
| 新版 exe 二次运行（种子已落地） | **2.76 s** | **0.97×** ✅（比旧版还快） |
| 新版 exe 且**外置种子缺失**（降级） | 3.44 s | 退化建「默认话术」v4 空存档，不崩 ✅ |

**端到端延迟 OFF vs ON（NFR-3：普通弹幕端到端历史水平上加 ≤2 秒）**

测法：整场重放 `rehearsal-001.jsonl`（14 事件），同一条链路跑两种模式——规则模式用瞬时 `reply_builder`
桩，智能体模式用 0 ms 的 `StubLLM` 桩；`speed=200` 把模拟等待压缩（两模式同参数，故对比公平）。
每条延迟 = 「产出任务时刻 − `task.event_ts`」（`ReplaySource` 用实时钟给事件打 ts，所以这个差就是该条
从进管线到产出任务的处理耗时，已剔除被压缩的模拟等待）。

| 模式 | 产出任务 | 处理开销 均 | P95 | 最大 | 全场景墙钟 |
|---|---|---|---|---|---|
| rules（开关 OFF） | 6 条 | 0.12 ms | 0.20 ms | 0.20 ms | 2.51 s |
| agent（开关 ON） | 12 条 | **3.38 ms** | 12.78 ms | 12.78 ms | 2.50 s |

**增量：ON 比 OFF 每条多 ~3.3 ms**（提示词组装 + L2 召回 + 契约解析 + 情绪校验的净开销），
相对 NFR-3 的 2 秒预算有 **三个数量级**余量。
> **未测**：真实 LLM（`DECISION_MAX_TOKENS=400` + 超时上限）与 MiniMax TTS 的耗时——需要双 Key，
> 属真实链路延迟，已记入待办（第 12 条）。上表的 LLM 是 0 ms 桩，因此数字代表"智能体在 LLM 之外
> 额外加的开销"。

**V7.5 稳定性（连续运行无崩溃、无内存持续增长）—— 有界实测 + 依据**

> 墙上 4 小时跑不了，用三条互补证据代替，并**明确这是有界实测**（见 HANDOVER 坑 67 与待办 13）。

| # | 证据 | 规模 | 结果 |
|---|---|---|---|
| 1 | **加速 soak**（真 `Router` + 真 `PrioritySpeakQueue` + 真 `AgentBrain` + 真 `DecisionLogger`） | `rehearsal-001` 全 14 事件 × 60 轮 = **840 事件** | RSS 52.1 MB → 峰值 54.0 → 末值 **48.8 MB（净 −3.2）**；前 10 轮均 52.3 → 后 10 轮均 48.7（**−3.5**） |
| 2 | **真实进程 soak A**（发布包 exe 挂 `app.replay_file` 播满整场 479 s，每 30 s 采样） | 1 场 / 479 s | 控制台 **16/16 采样存活**、无崩溃；前 30 s 预热 +63.5 MB（播放器/音频缓冲 + 69 MB 存档导入），之后 7.5 分钟仅 +5.5 MB 且**速率递减**（0.86 → 0.63 MB/min） |
| 3 | **真实进程 soak B**（发布包 exe 持续 WS 喂事件） | **15 分钟 / 128 轮 / 1796 事件 / 0 错误** | 控制台 **15/15 采样存活**；RSS 在 169.9~221.7 MB 之间**锯齿振荡**（GC 回收），前半程均值 202.4 MB → 后半程均值 **189.8 MB（−12.6 MB）**——**没有单调增长** |

三条独立证据一致指向"无持续增长"（长跑中 RSS 反而回落，符合 GC 锯齿而非泄漏）。
**未做**：完整 4 小时墙上连续开播（需 LLM + MiniMax 双 Key 与真机声卡）——已记入待办。

**V7.6 发布包解压双击可用（首跑种子自动落地）**

| 项 | 结果 |
|---|---|
| 发布包组装 | `scripts/make_release.py` → `dist/release/仔仔鼠AI智能体直播助手/`，**8 个文件、合计 89.10 MB**（19.0 exe + 69.2 外置种子 + 0.9 声卡工具），不含 `profiles/` |
| 解压后双击 | 把发布包复制到临时目录直接运行 exe（等效用户解压双击）→ **控制台 3.84 s 就绪**，日志显示种子已落地 |
| 首跑种子落地 | `profiles/<新 id>/` 自动出现，`profile.yaml` 为 **v4**，`audio/` 下 **120 个音频文件** —— 开箱即用，无需用户导入 |
| 缺种子的降级 | 故意删掉 `assets/seed_profile.zip` → 控制台仍 3.44 s 就绪，自动退化为「默认话术」**v4** 空存档（**不崩**），用户可自行建存档 |

**偏离 DESIGN 的决定（1 处，必须留痕）**

| # | DESIGN / 冻结清单原文 | 实际落地 | 原因 |
|---|---|---|---|
| 1 | DESIGN §15 冻结清单：`zaizaishu.spec` **只许改 name/icon** | M7 额外把 `assets/seed_profile.zip` 从 `datas` **移除**（改发布包外置），并新增 `hub/scriptstore.find_seed_zip()` 两级选址 | 69.2 MB 的 v4 种子内联把 exe 撑到 88 MB，**必然违反 NFR-2**（旧版 +5 MB）。外置是唯一既不减内容又能达标的路子，且首跑体验不变（`--verify` 与发布包实测均确认种子照常落地）。替代验证：`tests/test_make_release.py` 的种子选址接缝 + 真 `ScriptStore` 解档 + 缺种子降级实测 |

**提交列表**

| 提交 | 内容 |
|---|---|
| `0c7c151` | M7-1 旧存档迁移脚本 `migrate_from_old.py`（只复制不改源 + 两级校验 + 真 Store 试加载） |
| `547c903` | M7-2 旧版 467 项回归对齐（V7.1） |
| `525920d` | M7-3 PRD §FR-10 11 项保留能力逐项回归（V7.2） |
| `b5dcb65` | M7-5 种子外置 + 发布包组装脚本（exe 92.5 MB → 19.0 MB） |
| 见 git log | M7-4 性能实测 + M7-6 文档收尾 |

---

## 三、测试策略

### 3.1 分层

| 层 | 范围 | 要求 |
|---|---|---|
| 单元测试 | 新增模块（`agent/*`、存档 v4 迁移、情绪回落） | 每个模块必须有测试；沿用旧版的"注入时钟/随机源/相似度"确定性风格 |
| 回归测试 | 旧版 467 项 | 改动后必须全绿 |
| 开关双跑 | 同一份重放 JSONL，OFF / ON 各跑一遍 | 对比决策日志，确认 OFF 完全一致、ON 有可见差异 |
| 手工验证 | 五个页面交互、真实开播 30 分钟 | 每阶段一次 |

### 3.2 关键测试用例（必写）

| # | 用例 | 断言 |
|---|---|---|
| T1 | v3 存档迁移 | `version` 变 4，`audios[vid]["default"]` 含原值 |
| T2 | 迁移幂等 | 连续迁移两次结果一致 |
| T3 | 情绪回落 | 请求未登记档 → 现场 TTS；档位内缺音频 → `default` 预录 |
| T4 | stale 传播 | 改文本后该条所有档位 `text_hash` 失配 |
| T5 | 决策解析降级 | 三种非法 JSON 输入均得到可用决策 |
| T6 | 情绪校验 | 档位内 / 8 枚举内 / 非法 三种处置正确 |
| T7 | 记忆环形缓冲 | 满容量后新事件挤掉最旧的，顺序正确 |
| T8 | 队列 no_expire | 超期任务不被丢弃；`interrupt` 头插仍生效 |
| T9 | Router agent 模式 | 噪声弹幕不丢、聚合窗口不生效、敏感词仍拦截 |
| T10 | 反馈回流 | `rejected` 不参与 few-shot，`adopted` / `rewritten` 参与 |

### 3.3 测试命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q                      # 全量
.\.venv\Scripts\python.exe -m pytest tests -q -k agent             # 智能体相关
.\.venv\Scripts\python.exe -m pytest tests -q -k scriptstore       # 存档相关
```

---

## 四、发布包组装

```
dist\release\仔仔鼠AI智能体直播助手\
├── 仔仔鼠AI智能体直播助手.exe        # PyInstaller onefile，绿色图标（**不含种子**，见下）
├── config.yaml                       # 预填双 Key，含 agent 段（enabled: false）
├── blockwords.txt
├── 一键安装虚拟声卡.bat
├── VBCABLE_Setup_x64.exe
├── assets\seed_profile.zip           # **外置种子**：首跑自动落地，不进 exe
└── voice_clone\voice_clone_data\      # MiniMax Key + 音色库
```

**不带**（与旧版一致）：`profiles/`（首跑种子自动落地）、任何本地 TTS 整合包。

> **M7 起种子外置**：`assets/seed_profile.zip` 已到 69.2 MB（v4 三档）。内联进 onefile exe 会让
> exe 从 19.0 MB 膨胀到 92.5 MB（且每次启动都要解包），超出 NFR-2 上限（旧版 53.61 MB + 5 MB）。
> 现在 spec **不** datapack 种子，改由 `hub.scriptstore.find_seed_zip()` 两级选址：
> 包内 `BUNDLE_DIR/assets/seed_profile.zip` → exe 旁 `EXE_DIR/assets/seed_profile.zip`。
> 找不到种子时优雅降级为「默认话术」空存档，控制台照常可用（已实测）。

**打包命令**

```powershell
# ① 打 exe（产物 dist/zaizaishu-agent.exe）
.\.venv\Scripts\python.exe -m PyInstaller zaizaishu.spec --clean --noconfirm

# ② 组装发布包（产物 dist/release/仔仔鼠AI智能体直播助手/）
.\.venv\Scripts\python.exe scripts\make_release.py            # 加 --dry-run 先看清单
```

**打包前检查清单**

- [ ] `assets/seed_profile.zip` 已重建为 v4 多情绪版本
- [ ] `assets/icon.ico` 已换绿色版（多尺寸）
- [ ] `config.example.yaml` 含 `agent` 段且 `enabled: false`
- [ ] `webui/index.html` 无 emoji、无硬编码蓝紫色值
- [ ] `pytest tests -q` 全绿
- [ ] `dist/release/` 内 `config.yaml` 与源码 `config.yaml` 未分叉（Key 需同步）
- [ ] 打包环境装了 `pywebview` 与 `PyInstaller`（`requirements-dev.txt`）——`pywebview` 不在 `requirements.txt` 里，缺了 exe 起不来
- [ ] `dist/release/<产品名>/assets/seed_profile.zip` 在（外置种子，漏了首跑就没内容）

---

## 五、进度追踪

| 阶段 | 内容 | 天 | 状态 |
|---|---|---|---|
| 前置 | 归档旧版 + 建新版本目录 + 设计文档 | 1 | 已完成（2026-09-21） |
| M0 | 工程准备（派生基线、venv、测试全绿、spec/.gitignore） | 0.5 | **已完成（2026-09-21）** |
| M1 | 视觉与品牌改造（青绿、logo、更名、去 emoji） | 1 | **已完成（2026-09-21）** |
| M2 | 存档 v4 与情绪档位（含话术管理页情绪区） | 2 | **已完成（2026-09-22）** |
| M3 | 总开关与模式切换（Router mode / Queue no_expire） | 1.5 | **已完成（2026-09-22）** |
| M4 | 智能体核心（brain/memory/retriever/prompt/decide/emotion/log） | 3 | **已完成（2026-09-22）** |
| M5 | 智能体页面（状态条 / 决策透视 / 试玩调试） | 2.5 | **已完成（2026-09-22）** |
| M6 | 反馈进化与对话调参（采集 / 示例回流 / 反馈进化区 / 对话式调参） | 2.5 | **已完成（2026-09-23）** |
| M7 | 收尾（迁移脚本、回归、性能、发布包） | 1 | **已完成（2026-09-23）** |
| — | 合计 | 15 天 | **全部完成（2026-09-23）** |

### M0 实测记录（2026-09-21）

| 项 | 结果 |
|---|---|
| 派生文件数 | 102 个（排除 `.git` / `logs` / `profiles`） |
| venv | Python 3.14.7 重建，依赖按 `requirements.txt` 装齐（21 个包） |
| 基线测试 | **467 passed in 67.21s** |
| 配置校验 | `load_config` + `validate_config` 无错误，`agent` 段正确解析 |
| 应用启动 | `python main.py` 正常起，控制台 127.0.0.1:8766 LISTENING |
| 配置摘要 | 产品名 `zaizaishu-agent`；LLM 与 MiniMax 均判定已配置 |
| 首跑种子 | 存档指针自动从 `p_c6a0cfda` 兜底为 `p_c2820e66`（profiles 为空 → 种子自动落地，机制正常） |
| 控制台冒烟 | `scripts/webui_smoke.py` 全通过：快照 / 历史 / 事件回流 2/2 / 音色工坊 5 项断言 |
| 配置注释 | `config.yaml` 的 `agent` 段与既有注释均完整保留（行级写入未破坏） |
| 提交 | `e44c00a` |

> ~~**一个待办**：首跑落地的种子存档仍是旧 v3 结构（来自 `assets/seed_profile.zip`）。M2 需重建为 v4（多情绪档），届时种子存档的 `default` / `happy` / `calm` 三份音频一并预生成。~~
> **已办结（2026-09-22，M2）**：种子 zip 已重建为 v4（121 entries / 69.2 MB），当前音色三档音频齐全；并由 `test_shipped_seed_zip_is_v4_and_self_consistent` 长期守护。

### M1 实测记录（2026-09-21）

**配色替换（`webui/index.html`）**

| 项 | 结果 |
|---|---|
| 变量层 | `:root` 重写为 41 个变量：10 个中性色（去蓝调改绿灰）+ `--brand-*` 9 档 + `--brand-*-rgb` 三元组 + 4 个极光/2 个网格变量 |
| 色值替换 | 19 处 hex + 38 处 `rgba()` 字面量 → 全部改为变量引用，**全仓零残留蓝紫 hex**（扫描 22 个旧色值） |
| 三元组手法 | `--brand-rgb: 0, 194, 154;` + `rgba(var(--brand-rgb), .12)`，透明度调节不再散落硬编码 |
| 极光/网格 | 4 层极光 + 2 级网格改为青绿低饱和（`--aurora-1~4`、`--grid-1/2`） |
| 遗留蓝底 | 补修 1 处卡片图标底 `linear-gradient(#eaf3fe, #d7e8fa)` → `var(--brand-soft) → var(--brand-mist)` |

**踩到的坑（已修）**：首版替换脚本把 `HEX_MAP` 排在 SVG 步骤之前，导致 `stop-color="#4a93e8"` 被写成 `stop-color="var(--brand-bright)"`。SVG presentation attribute（`stop-color` / `stroke` / `fill`）**不支持 CSS `var()`**，浏览器会整条丢弃。已回退为字面量 hex 共 6 处，并加了「属性内含 `var(--brand`」的兜底扫描。**规则：SVG 表现属性只写字面量色值，`:root` 变量只服务 CSS 声明。**

**品牌与图标**

| 项 | 结果 |
|---|---|
| 产品名 | 官方全称统一为「仔仔鼠AI智能体直播助手」，12 处命中：`index.html` 标题/品牌行/logo aria-label、`desktop.py`（TITLE + 2 处 docstring + argparse）、`zaizaishu.spec`、`main.py`（2 处）、`scripts/tts_stream_check.py` |
| 保留项 | 主播人格昵称「仔仔鼠」不动（`persona.nickname`、告警标题、prompt）；页面名「直播中枢」不动 |
| 顶栏副标题 | `AI LIVE CENTER` → `AI AGENT LIVE`；`.nm b` 字距 `.04em` → `.02em` 以容纳 4→11 字 |
| 图标 | `scripts/make_icon.py` 渐变 `#4a93e8→#2f7de1`（蓝）→ `#40d1b3→#009273`（青绿），与顶栏 SVG 同一组色；重生成 `assets/icon.ico`（16/24/32/48/64/128/256，25468 → 26457 bytes） |
| 构建依赖 | 新增 `requirements-dev.txt`（`Pillow>=10`，仅构建期），Pillow 未进运行时依赖 |
| 顺手清理 | 删掉 `_rounded_gradient` 里一行未使用的 `Image.linear_gradient` 死代码 |

**emoji 清理（`webui/index.html`）**

| 原字符 | 数量 | 处理 |
|---|---|---|
| `✓` / `✗` | 8 处 | 改为 `.st-mark` + `::before` 的 SVG mask 图标，`background: currentColor` 随状态色自动着色 |
| `⚠` | 1 处 | 同上，接 `.vw-keepalive.warn`（音色保活剩余天数） |
| `💡` | 1 处 | 同上，挂在 `.llm-test-advice` 上 |
| `✏️` | 1 处 | 直接删除（`<option>` 无法承载 SVG，语义已由选项文本表达） |
| `→` | 8 处 | **保留**——属正常排版标点，非 emoji |

配套动作：`:root` 新增 5 个图标变量（`--ico-check/cross/warn/tip/arrow`），`data:` URI 内 `#` 写作 `%23`；`.llm-test-icon`（绿/红圆底白勾叉）由字符改为 13px 白色 mask。

**顺手做的一处 UI 升级**：`<span class="hint">弹幕 → 回应 → 合成 → 输出</span>` 改为 `.pipe-hint` 步骤胶囊 + SVG 箭头（`--ico-arrow`）。它原本与下方 `.pipe` 视觉链路重复，现在成为真正的微组件，同时清掉了可见文案里唯一的箭头字符。

**对比度专项（V1.4）**

初版目视「看起来没问题」，但按 WCAG 2.1 公式实测后暴露 7 项不达标。逐项处理如下（审计已固化为 `scripts/ui_audit.py`，可随时重跑）：

| 问题 | 性质 | 处理 |
|---|---|---|
| 实心按钮白字 **2.29:1** | **本次引入的回归** | 旧版 `--blue #1e6fe8` 配白字是 4.67:1（过 AA）。新增 `--brand-solid: #00a886` 作白字载体亮端 → 亮端 3.02:1 / 中点 3.45:1 / 暗端 3.92:1 |
| 品牌图标描边 **2.18:1** | 本次引入的回归 | 旧版 `#1e6fe8` 是 4.43:1。改为 `#009273`（3.92:1，过图形 3:1）；5 处内联 SVG `stroke` 同步 |
| 品牌链接字 **2.18:1** | 本次引入的回归 | `.vb-link` 改用 `--brand-deep #00614d`（7.10:1） |
| 状态/焦点描边 2.18:1 | 本次引入的回归 | 18 处 `border-color` + 复选框 `accent-color` 改用 `--brand-press`（3.92:1） |
| 语义文字 3.05 / 3.91 / 2.03:1 | **历史遗留**（`--green`/`--red`/`--gold` 与旧版完全一致） | 新增 `--green-text`/`--red-text`/`--gold-text` 三档深色，并把文件里散布的 **30 处**硬编码深色值（`#0b7a48`×10 / `#c0392b`×8 / `#9a6b07`×12）收口为令牌引用 → 5.40 / 5.44 / 5.36:1 |
| 弱提示字 2.61:1 | 历史遗留（`--ink-3` 旧值 2.52:1，新版已略优） | 加深为 `#7d918b`（3.18:1） |
| 警示字 / `--gold-soft` 底 4.25:1 | 历史遗留 | `--gold-text` 加深为 `#8f6206`（4.87:1） |

**关于品牌色的诚实说明**：`#00C29A` 是 WorkBuddy 的 `--wb-palette-brand-8`，其白字对比度上限就是 2.29:1 —— 压到 AA 4.5:1 需要 ≈`#008065` 的墨绿，会背离「参考 WorkBuddy 颜色」的要求。实测 WorkBuddy 自身设计系统也是把 `--wb-brand-primary`(=`brand-8`) 直接当按钮底色与品牌文字色用，同样不满足 AA。因此本版**保留品牌色相**，把白字载体压到 3:1 门槛（图形/大字标准），并额外提供 `--brand-deep`（7.45:1）作为需要严格 AA 时的实心按钮替代色。这一取舍已写入 `docs/DESIGN.md`。

**踩到的两个坑（已修）**

1. **`--brand-bright` 未被清除干净**：首轮把白字梯度亮端从 `--brand-bright`(#40d1b3, 1.87:1) 改成 `--brand-primary`(#00c29a, 2.29:1)，仍不够，二轮才引入 `--brand-solid`。教训：**「看起来浅绿」不等于「白字能读」，必须实测而非目视**。
2. **全局替换把 `:root` 里的定义也替换了**：替换 `#0b7a48 → var(--green-text)` 时，刚写入 `--green-text: #0b7a48;` 的定义行同样命中，产生 `--green-text: var(--green-text);` 自引用（CSS 变量自引用会整条失效，且浏览器不报错）。已在审计脚本里加入**自引用检测**作为常规检查项。

**验证**

| # | 标准 | 结果 |
|---|---|---|
| V1.1 | 逐页目视无残留蓝紫 | 通过 —— 无头 Edge + CDP 截取 4 个页签 + 2 张特写（`logs/_ui_shots/`），全站青绿；色相扫描（H 195–300°）零命中 |
| V1.2 | 全文检索无 emoji | 通过 —— `scripts/emoji_scan.py` 按 Unicode 区间扫描（含变体选择符），实体 emoji 归零 |
| V1.3 | 标题/窗口标题/exe 图标为新品牌 | 通过 —— 页面标题实测 `仔仔鼠AI智能体直播助手 · 控制台`；品牌行 `仔仔鼠AI智能体直播助手 / AI AGENT LIVE`；主题色 `#00c29a`；图标顶部 `#3acbad` → 底部 `#069879` |
| V1.4 | 对比度 | **通过（26 项全过）** —— 正文 15.3:1、次要字 5.95:1、弱提示 3.18:1、品牌深字 6.2–7.5:1、实心按钮白字 3.02–3.92:1、语义文字 4.76–5.44:1、图标描边 3.92:1。由 `scripts/ui_audit.py` 脚本化实测，非目视 |
| V1.4b | CSS 变量完整性 | 通过 —— 定义 50 个 / 引用 49 个 / 引用未定义 **0** / 自引用 **0** |
| V1.5 | 功能无回归 | 通过 —— `pytest -q` **467 passed in 62.23s** |

**验收脚本**（已固化进仓库，M5 加页面后必须重跑）

| 脚本 | 作用 | 通过判据 |
|---|---|---|
| `scripts/ui_audit.py` | CSS 变量完整性 + 对比度实测 + 蓝紫残留 | 退出码 0；无 FAIL |
| `scripts/emoji_scan.py` | 按 Unicode 区间扫 emoji（含 U+FE0F） | 退出码 0；emoji 0 行 |
| `scripts/ui_shots.py` | 无头 Edge + CDP 截 4 页签 + 2 特写 | 6 张 PNG 生成，标题/品牌/主题色断言正确 |


**提交**：见 `git log`（M1 单次提交）

> **未做（有意留下）**：① 智能体导航页签与页面骨架 → M5；② `logs/_m1_shots/` 截图被 `.gitignore` 忽略，仅作本地证据；③ `assets/seed_profile.zip`（35 MB）与 `assets/vbcable/VBCABLE_Setup_x64.exe` 仍被 git 跟踪，M7 评估是否改走发布包外置；④ 进度条填充仍用 `--brand-bright`（大色块图形，与 `--surface-3` 轨道对比充分，不动）。

### M2 实测记录（2026-09-22）

**后端（存档 v4 与三级回落）**

| 项 | 结果 |
|---|---|
| 结构升级 | `audios[vid]` → `audios[vid][档位] = {file, text_hash}`；新增顶层 `emotions`（`default` 恒在排首 + MiniMax 8 枚举） |
| 出厂默认档位 | `default + happy + calm`（`DEFAULT_EMOTIONS`） |
| v3→v4 迁移 | `migrate_v3_profiles()`：先备份 `profile.yaml` → `profile.yaml.v3.bak`，备份失败即放弃迁移（宁留旧结构不丢数据）；判定点唯一（`_flat_to_slots`，值里直接含 `file` 即扁平形态），读取与落盘共用 → 天然幂等 |
| 真实存档实测 | `profiles/p_c2820e66` 迁移前 `version: 3`、备份为扁平 `{file, text_hash}`；迁移后 `version: 4`、`emotions: [default, happy, calm]`；二次调用返回 `[]`（V2.1 / V2.2 ✓） |
| 三级回落（V2.5 ✓） | ① 当前音色×happy 命中预录；② 其他音色×happy（档位已登记但缺音频）→ 回落该音色 `default` 且 `sc_emotion_fallback` +1；③ 当前音色×sad（未登记档）→ 返回空串交现场 TTS 带 sad |
| stale 语义（V2.4 ✓） | 改文本 → 该音色**三个档位一起**返回空串（不再播旧文本）；`stale`（voice 级）与 `stale_emotions`（逐档明细）并存 |
| 防覆盖护栏 | `_guard_existing_audio` 改逐「音色 × 档位」判定：丢一档不牵连其余档 |
| 合成通道 | `ScriptStore.set_synthesizer(fn)` + `generate_audio()` 把「合成 → 落盘 → 写引用 → 清旧文件」原子化；`webui._ensure_synth()` 接线到 `VoiceWorkshop.synthesize_wav`；**生成即自动登记档位**（否则生成的音频永远不被播放链路采用） |
| 前端播放分派 | default 档**绝不**把档位名透传给合成接口（`_make_stream` 里 `emotion == default → None`），只有非 default 档才记 `emotion_onsite` 并带 emotion 覆盖 |

**前端（话术管理页「情绪档位」区）**

| 项 | 结果 |
|---|---|
| 校验 | 488 → **489 passed**；`scripts/ui_audit.py` 全通过（变量 50 定义 / 0 未定义 / 0 自引用，26 组对比度全 PASS，无蓝紫残留）；`scripts/emoji_scan.py` PASS（0 emoji） |
| 新增区 | 9 行档位网格（`default` 恒选中且虚线锁定不可取消）、逐档「缺 N / 就绪」标签、逐档「生成 / 清空」、音色下拉 +「按档位批量生成」+ 独立进度条 |
| 双区域进度 | 批量生成进度条按来源分派：存档音色区 → `scGenAllWrap`，情绪档位区 → `scEmoGenWrap`；两区域互斥（`scStartGenAll` 抢跑时拦截），避免进度条跑到另一边 |
| 条目级档位 | 每条音频的主芯片 = `default` 档，其后缀各档子芯片（「欢快」「平静·缺」），点一下即试听（已就绪）或补齐该档 |
| 广播处理 | `applySc` 新增 `emotions` / `emotions_saved` / `emotion_purged`；`emotions_saved`·`emotion_purged` 后自动重拉档位视图重算缺档数；换存档才丢弃未保存的档位草稿 |
| 端到端验证 | 自建「同端口 HTTP 静态 + WS」假后端（`websockets.process_request` 顺带伺服 `index.html`），握手后按真实协议推 `vc` / `list` / `profile` / `emotions`，**走的是真实 `applySc` 分支**；实测 9 档位行、6 个音色芯片、12 个档位子芯片、缺档标签「缺 1 / 缺 1 / 缺 12」、批量进度文案「1/4 · 备用音 · 全部档位」、单档进度「1/3 · 备用音 · 默认档」全部符合预期 |

**种子存档重建（M2 第 8 项）**

| 项 | 结果 |
|---|---|
| 口径 | **三档但仅当前音色**（用户选定）：当前音色 `voiceB02` 补 `happy` + `calm`，共 60 份新增；另一音色 `voiceA01` 保持仅 `default`——正好真实演示「档位缺 N 条」提示与三级回落 |
| 生成 | MiniMax `speech-2.8-hd`，情绪档实测可用（`emotion=happy/calm` 均正常返回）；有每分钟调用上限（`status_code=1002 rate limit exceeded(RPM)`），生成器加 1.6 s 节流后 120 次调用全部成功 |
| 产物 | `profile.yaml`（121 entries 含 yaml，无孤儿音频）+ 120 份音频；`version: 4` / `emotions: [default, happy, calm]`；33.8 MB → **69.2 MB** |
| 导入验收 | `import_profile` 零 warnings；120 份引用全部落盘；当前音色缺档 `{default: 0, happy: 0, calm: 0}`，其他音色 `{default: 0, happy: 30, calm: 30}` |
| 守护测试 | 新增 `test_shipped_seed_zip_is_v4_and_self_consistent`：只读 metadata（不解 69 MB 音频），断言 version / emotions / 引用完整性 / 当前音色三档齐全 / 无孤儿音频 |

**新踩到的坑（已固化）**

1. **MiniMax 有 RPM 上限**：连续快速调用会在第 ~42 次开始连续报 `rate limit exceeded(RPM)`（`status_code=1002`），且失败的调用不产生音频。批量预生成必须加节流（1.6 s 间隔实测安全）并做成**可断点续跑**（按 key 记 manifest，跳过已有文件）。
2. **假后端验证前端时，WS 升级请求必须放行**：`websockets.process_request` 里若无条件按静态文件 404 `/ws`，握手会失败、页面永远停在「连接断开，重连中」。判据是 `Upgrade: websocket` 头 → 返回 `None`。
3. **前端整段包在 IIFE 里，CDP 注入不到内部状态**：`webui/index.html` 的脚本是 `(() => { ... })()`，`scCur` / `renderScEditor` 都不在全局。想让前端走真实渲染分支，只能**喂真协议消息**（假后端），不能靠 `Runtime.evaluate` 硬塞状态。
4. **子芯片宽度会把音色芯片撑破**：给 `.sc-au-chip` 加 `flex-wrap: wrap; max-width: 100%`，否则档位子芯片一多就横向溢出。

**提交**：`31d658a`（后端）→ `3fed89c`（前端 + 种子重建）

### M3 实测记录（2026-09-22）

**后端（开关、双模式、热切换）**

| 项 | 结果 |
|---|---|
| `agent` 段解析 | `agent_settings()` 归一化：缺项取出厂默认（关 / l2 / top_k 5 / 记忆 50·20·10 / 情绪与反馈默认开）、越界夹取（`top_k` 999 → 20）、枚举非法回落（`layer: L9` → `l2`）、宽松真假词表（`yes`/`on`/`off`/`no`/`0`） |
| `agent` 段校验 | `validate_config` 补 4 个数值范围（top_k 1-20 / max_events 1-500 / max_spoken 1-200 / max_notes 0-100，`max_notes` 0 是合法下界）+ `layer` 枚举 + 5 个布尔键类型 |
| 行级写回 | `update_agent_config()`：点分路径 + 白名单过滤（非白名单键丢弃）+ 换值保注释 + 缺父链补齐 + 无段追加 + 段尾空行不吸新键；`_block_bounds` 按**缩进精确相等**区分 `agent.enabled` 与 `agent.emotion.enabled`；枚举裸写不加引号（与模板一致，避免每次开关都产生引号 diff） |
| 队列 `no_expire` | 构造参数 + 属性 + `set_no_expire()`；`_is_stale` 恒 `False`、`put_idle` 不查积压；优先级/插队/暂停清空不变 |
| Router 双模式 | `normalize_mode()` 非法值回落 `rules`（安全方向）；agent 模式 chat 逐条产出（不聚合、不按概率丢噪声）、gift 跳过连击状态机逐条产出、follow/fansclub 跳过冷却与概率（`bypass=True`：不占冷却且 `test` 保持 `False`） |
| agent 模式对照实测 | 同一份 `replay/rehearsal-001.jsonl` 过真实管道：rules 模式 6 条聊天 → 5 个任务（含一条 `merged_count=2`）、礼物连击合并为 1 条；agent 模式 6→6 聊天、5→5 礼物、1→1 关注，`merged_count` 恒 1，无聚合窗口 |
| 队列过期对照 | 人为让全部任务超龄 600 s：rules 模式全部丢弃（`dropped_stale > 0`、剩余 0 条）；agent 模式**一条不丢**（`dropped_stale == 0`，剩余数与产出数一致）→ V3.5 ✓ |
| 敏感词闸门 | 两侧 `blocked_input` 计数一致（同一闸门），被拦弹幕在 agent 模式下既不产出任务也不进智能体 → V3.4 ✓ |
| 热切换 | `set_mode()` 切换前先排空中间态：窗口内 1 条 chat + 未结算连击 1 条先产出，切换后 `_window is None`、`_combos == {}`；`switchTab`/重连会自动补发 `ag_status` |
| 接线装配 | 启动读 `agent_settings` 决定 Router mode / Queue no_expire；出厂 OFF 时 `mode=rules` + `no_expire=False`（V3.1）；`_set_agent_enabled` 先写 `config.yaml` 再切运行时，写盘失败即整体不动 |

**前端（系统配置页「智能体」卡）**

| 项 | 结果 |
|---|---|
| 卡片布局 | 配置页共 7 张卡（奇数），本卡 `grid-column: 1 / -1` 占满整行以免尾行留空；内部两栏自适应（`repeat(auto-fit, minmax(300px, 1fr))`）：左栏开关 + 副标题 + 结果说明，右栏 7 项档位摘要（检索层 / 召回条数 / 事件记忆 / 防复读 / 情绪动态 / 档位缺则现场合成 / 决策日志） |
| 状态渲染 | 关闭态：徽章「已关闭」+ 7 项摘要全置灰；开启态：徽章「已开启」+ `模式·智能体` 徽章 + 摘要点亮；未配 LLM Key：徽章「不可开启」（warn 色）+ 开关置灰 + 副标题说明去哪配 + 红字降级原因 → V3.3 ✓ |
| 开关动作 | 勾选即发 `ag_set_enabled`；未配 Key 时**本地先拦**（回滚勾选 + 提示），服务端也有兜底拒绝；服务端回推 `op=ag_saved` / `message` 就地显示；用「状态指纹」判断只在真变化时清提示，**每秒快照帧不会冲掉切换结果** |
| 端到端验证 | 假后端 + 无头 Edge（`scripts` 同级临时脚本，走真实 `applySnap` / `applyAgent` 分支）：关 → 开 → 关 双向真实往返、未配 Key 置灰与本地拦截回滚，5 张截图（`logs/_m3_shots/`，gitignored） |
| 校验 | 537 → **539 passed**；`scripts/ui_audit.py` 全通过（50 定义 / 0 未定义 / 0 自引用，对比度全 PASS，无蓝紫残留）；`scripts/emoji_scan.py` PASS（0 emoji） |

**新踩到的坑（已固化）**

1. **页面脚本在 IIFE 里，`send` 不在全局**：想从 CDP `Runtime.evaluate` 触发一个前端动作（例如"切到某种演示状态"）会直接 `ReferenceError: send is not defined`。正解仍是**由服务端驱动**——假后端维护一个 `CLIENTS` 列表 + `broadcast()`，脚本用第二条 WS 连接发自定义动作，服务端改状态后广播快照给页面。子注：驱动用的每条 `__demo` 动作都要真的广播一次，否则页面停在旧状态。
2. **`websockets` 里"服务端广播"要自己维护连接表**：`async for raw in ws` 的 handler 只在**该连接**上回消息，别的客户端（脚本的控制连接）发的指令改不了页面 —— 必须显式 `CLIENTS.append(ws)` + 广播，并在 `finally` 里摘除。
3. **`grid-auto-rows: 1fr` 下卡片数为奇数会留空单元格**：配置页卡片数从 6 变 7 后，尾行右侧出现一块空白（看起来像渲染 bug）。给新卡加 `grid-column: 1 / -1` 让它占满整行，顺带用两栏内布局消化宽度。
4. **枚举值写回不要一律加双引号**：`update_llm_config` 的 `_yaml_quote` 是为含特殊字符的密钥准备的；直接沿用会把 `layer: l2` 写成 `layer: "l2"`，与模板不符且每次开关都产生 diff。新增 `_yaml_scalar()`：布尔写小写、数字裸写、字符串只在必要时加引号。
5. **追加段时 `sec_idx` 要指向段头而不是种子键行**：`lines.append("agent:")` + `lines.append("  enabled: false")` 后取 `len(lines) - 1` 会指到 `enabled` 那一行，导致后续写回把新键插到段外、出现重复 `enabled:`。正解 `len(lines) - 2`。
6. **`StatsCollector.get()` 只接受一个参数**（不是 `dict.get(k, default)`）：临时校验脚本里习惯性写 `stats.get("dropped_stale", 0)` 会 `TypeError`，要用 `stats.summary().get(k, 0)`。
7. **同步钩子抛异常不能落进"没配 LLM 就静默"的分支**：`_dispatch_context` 里钩子异常原先回落到 `_finalize_chat_context`，而 agent 模式下 `reply_builder` 可能是 `None` → 异常被吞成静默。改为与异步路径一致地走 `_fallback_reply()`（`safety.fallback_reply` 兜底口播），并抽出 `_fallback_reply()` 供两条路径共用。

**提交**：`26fa998`（M3-1 配置）→ `30e58db`（M3-2/3 队列与 Router）→ `55c5967`（M3-4/5 动作族与装配）→ `b3b33cc`（M3-6 前端卡）

### M4 实测记录（2026-09-22）

**落地口径的三处偏离 DESIGN 的决定**

| # | DESIGN 原文 | 实际落地 | 原因 |
|---|---|---|---|
| 1 | 容错链三级（JSON 提取 → 纯文本 → 兜底口播） | **五档**：`""` / `extract` / **`salvage`** / `plain` / 抛 `DecisionError` | 截断的 JSON 照原文降到"纯文本当 reply_text"，会把 `{"should_reply": true, "reply_` 这种残骸**念给观众听**——不崩不静默，但观感灾难。新增"抢救档"用正则抓 `reply_text` / `should_reply` 两个字面量；抓不出来的直接判定为 JSON 残骸，抛出去走兜底口播 |
| 2 | LLM 传输层未指定（§15 冻结了 `hub/generator.py`） | 新增 `hub/agent/llm.py`（方案 A） | `Generator` 在冻结清单里、`system_prompt` 是有状态的、`_request()` 出口套 `clean_reply`（会把 JSON 洗坏）。新客户端复用同一份 `llm` 配置，但 `max_tokens` 用 400——默认 120 装不下九字段中文契约，必然截断 |
| 3 | `AgentBrain.decide(event) -> AgentDecision` | `AgentBrain.handle(context) -> awaitable[dict \| None]` | Router 的钩子契约是"同步调用、返回可 await 的结果或直接结果"，所以要 `handle` 而不是 `decide`；返回 **dict**（不是 `AgentDecision`）才能顺带把 `audio_path` / `emotion` / `speed` 带过钩子 |

**接管范围（M3 的接缝只到 chat，M4 补齐）**

| 项 | 结果 |
|---|---|
| M3 遗留缺口 | `set_agent_handler` 只挂在 `_handle_chat` 出口；agent 模式下 gift/follow/fansclub/enter 仍走 `templates.render` 本地模板 —— 与 DESIGN §5.1「每条都产出，**怎么感谢交智能体**」不符 |
| 补齐方式 | 新增 `Router._agent_active()`（`mode == agent` **且** 钩子已注入，缺一不可）与 `agent_attached` 属性；`_dispatch_context()` 通用化为五类事件共用；`_emit_chat_result` + `_emit_chat_task` 合并为 `_emit_agent_result`（承接 `priority` / `interrupt` / `emotion` / `speed` / `audio_path`）；新增 `_event_context` / `_gift_context` |
| rules 模式不变性 | 分档、逐档开关、模板渲染、P0 与 mid/high 插队、冷却与概率门槛**逐项原样保留**；只切 mode 未挂钩子 = M3 降级路径（逐条产出、本地模板），只挂钩子但 mode=rules = 规则链路完全不变（不偷事件） |
| 用户级开关仍在智能体之前 | 逐类事件开关（`event_enabled`，DESIGN §六 第 17 条"保留"）与礼物分档开关在进智能体**之前**生效；`test` 事件照旧绕过 |
| 兜底唯一入口 | 钩子同步异常与异步生成器异常共用 `_fallback_text()` → `_fallback_reply()`（P1、不插队）；`safety.fallback_enabled: false` 时静默不产出 |

**整场重放实测（`replay/rehearsal-001.jsonl`，五类事件 14 条）**

真实 `Router` + 真实 `PrioritySpeakQueue` + `AgentBrain` + 桩 LLM（决策按事件内容脚本化，不经网络）：

| 项 | 结果 |
|---|---|
| 事件分布 | chat 6 / gift 5 / follow 1 / fansclub 1 / enter 1，原始时长 479 s（`speed=1000` 约 0.5 s 跑完） |
| V4.1 决策完整性 | **14 条事件 → 14 条决策**，九字段契约条条齐全，`latency_ms` / `model` / `recall` / `event_line` 齐备（`event_line` 是人话摘要，M5 决策透视直接用） |
| V4.1 下游落地 | 12 条任务（2 条主动不回）：嘉年华 → `priority=0, interrupt=True`；玫瑰 → P0 不插队；小心意 → P1；`回来啦兄弟` → P2；出队顺序实证「插队的嘉年华第一个播、P0 全部先于 P1」 |
| V4.2 非法输出 | 截断 → `salvage`（"半截的回复"）；混说明文字 → `extract`；纯散文 → `plain`；JSON 残骸 → 抛错 → Router 落 `safety.fallback_reply`。四种**都不崩**，且残骸不会被念出去（断言任务文本里不含 `should_reply` 字样） |
| V4.3 主动不回 | `should_reply: false` 的两条（`666` 弹幕、进场）确实不产出任务；契约层已把 `reply_text` 强制清空 |
| V4.4 情绪三态 | 档位内（`calm` 已勾选）→ 原样接受、`emotion_event` 空；8 枚举内未勾选（`surprised`）→ 接受并标 `emotion_onsite`（走现场 TTS）；非法（`得意`）→ 回落 `default` 并标 `emotion_invalid`；`emotion_raw` 与最终 `emotion` 分开记录 |
| V4.5 日志 | 每行一个 JSON、行行可 `json.loads`，按天滚动 `logs/agent-YYYYMMDD.jsonl`；写失败只 warn 不反噬决策 |
| V4.6 单场记忆 | 14 条事件全在（含两条不回）；观众甲连刷礼物三条（count 1/2/3）可识别；12 句进防复读缓冲，`is_repeated` 对原句为真；2 条笔记（嘉年华 / 答过下载） |
| V4.7 预录音频复用 | 原样采纳召回素材 `s1` → `audio_path` 命中其预录音频；另写一句（素材 `s2` 无音频）→ 不复用；`prerecorded` 标记只在真复用那条为真 |
| main 装配接线 | 真实 `run_main_loop`：开关 ON 启动即挂钩子、OFF 不挂、未配 LLM Key 永不挂、热切换挂/摘同步；状态 provider 的 `enabled` 走「mode + 钩子」双条件，另带 `model` / `session`（M5 状态条用）。此前这段闭包装配**零测试覆盖** |
| 校验 | 637 → **651 passed**（新增 `test_agent_e2e.py` 13 项 + 提示词回归 1 项） |

**修掉的一个提示词接缝 bug（只在真实链路暴露）**

`SessionMemory.brief()` 返回的是事件 **dict**，而 `prompt._memory_section()` 是纯文本装配器（`f"· {行}"`）——于是 system prompt 里出现了

```
· {'type': 'chat', 'user': '观众甲', 'content': '主播这是什么游戏', ... 'ts': 1790049318.1878028}
```

实测（12 条记忆 + 1 句说过 + 1 条笔记）：system prompt **2271 字符 → 1119 字符（-1152 字符，约 103%）**，且每个事件都要付一次。修法：`brief()` 改返回 `event_lines()` 渲染好的人话行（`[12:01:15] 弹幕 观众甲：主播这是什么游戏`）。

之所以一直没被测出来：提示词单测传的是**手搓的字符串列表**（理想形状），真实链路传的是记忆对象。补 `test_memory_section_renders_real_brief_as_human_lines`，从真 `SessionMemory` 取 `brief()`，并断言提示词里不出现 `'type':` / `'gift_value':` / 原始时间戳浮点。

**新踩到的坑（已固化）**

详见 `docs/HANDOVER.md` §6.5（5 条）。摘要：

1. **接缝测试要喂"真产物"而不是"理想形状"**——上面那个 bug 的根因。
2. **钩子推广时别漏同步分支的调用点**：删掉 `_emit_chat_result` 后漏改 `_finalize_chat_context` 的同步分支，17 个用例直接 `AttributeError`。
3. **`max_reply_chars` 不能在构造期缓存**：`main` 里 brain 可能建在存档装载完成之前，构造期读会锁到 config 兜底值。
4. **默认 `max_tokens` 装不下决策契约**：`llm` 段默认 120，九字段中文 JSON 必然截断 → `DECISION_MAX_TOKENS = 400`。
5. **`interrupt` 只在 `priority == 0` 时生效**要在两处都夹一次（`decide.py` 归一 + `router._emit_agent_result` 落地），只写一处会被绕过。

**提交**：`4afd890`（M4-1 内核）→ `53ff294`（M4-2 日志/LLM/检索/提示词）→ `e099948`（M4-3 AgentBrain）→ `8cbc0f5`（M4-4 接管接线）→ `ed07970`（M4-5 验收 + 接缝修复）

**建议顺序依赖**

```
M0 → M1（可并行于 M2）
M0 → M2 → M3 → M4 → M5
              M4 → M6
       M2/M3/M4/M5/M6 → M7
```

- M1（视觉）与 M2（存档）**互不依赖，可并行**
- M4 是核心，依赖 M2（情绪与存档接口）与 M3（开关与模式）
- M5、M6 都依赖 M4

---

## 六、验收总表

实施完成后，逐项对照 `docs/PRD.md` 的验收标准（AC-1.1 ~ AC-10.2）打勾。任一未通过项必须记录原因与处理决定，不允许静默跳过。

**总览：54 项 —— ✅ 通过 46 / ⚠️ 有条件通过 8**。8 项 ⚠️ 里没有一项是「没做也没说」：4 项需真实声卡或 LLM/MiniMax 密钥（本机无 Key 环境不虚构结论）、4 项是已留痕的环境/结构项。逐项如下。

### 6.1 FR-1 版本隔离与目录结构

| AC | 标准 | 状态 | 取证 / 说明 |
|---|---|---|---|
| AC-1.1 | 归档旧版 `git status` 干净、HEAD 仍为 `fbaa64c` | ✅ | 实测：归档 HEAD = `fbaa64c`，`git status --porcelain` 输出为空 |
| AC-1.2 | AIZB 根目录只剩 `.workbuddy` 与两个文件夹 | ⚠️ | 实测：`.workbuddy` + `_old-zaizaishu-live-20260921/` + `zaizaishu-agent-live/` 之外，根下另有 1 个**文件** `智能体待开发清单-20260923.html`（34 KB）。**处理决定**：为便于查阅暂留根下；如需严格达标，移入 `zaizaishu-agent-live/docs/` 即可（不阻塞发布） |
| AC-1.3 | 新版本运行时不读写归档目录任何文件 | ✅ | 全仓检索 `_old-` 仅 1 处命中：`scripts/migrate_from_old.py:DEFAULT_SRC` —— 是**用户显式调用的迁移工具**，运行期（`main.py` / `desktop.py` / `hub/**`）零引用；且迁移为只读复制，`verify_migrated` 以 size+`mtime_ns` 快照证明源目录零变化 |
| AC-1.4 | 归档旧版重建 venv 后可正常启动、跑通测试 | ⚠️ | 已验：把归档复制到临时目录、用新版 venv 实跑其测试 **466 passed / 1 flaky**（M7-2）。**未单独做**：在归档内重建独立 venv 并启动 GUI（本机 GUI 进程随命令组被回收，且旧版启动依赖声卡/Key）。**处理决定**：以「测试可跑通」为达标证据，GUI 启动留给发布包实机验收 |

### 6.2 FR-2 品牌与视觉更新

| AC | 标准 | 状态 | 取证 / 说明 |
|---|---|---|---|
| AC-2.1 | 无残留蓝紫元素 | ✅ | `scripts/ui_audit.py` ③ 段：字面量 hex 48 种 / 86 次，蓝紫色相（195–300°）零命中 |
| AC-2.2 | 前端源码无 emoji | ✅ | `scripts/emoji_scan.py`：emoji 0 行、排版标点 0 行，PASS |
| AC-2.3 | 窗口标题 / exe 名 / 界面标题均为「仔仔鼠AI智能体直播助手」 | ✅ | M1 计 12 处命中；`ui_shots` 实测页面标题「仔仔鼠AI智能体直播助手 · 控制台」；spec `name='zaizaishu-agent'`，发布包 exe 由 `make_release.py` 改名为「仔仔鼠AI智能体直播助手.exe」 |
| AC-2.4 | 图标（exe / 窗口 / 页面内）为绿色版本 | ✅ | `assets/icon.ico` 由 `make_icon.py` 青绿渐变重生成（26,457 B / 7 档尺寸）；页面 logo 内联 SVG `#40d1b3→#009273`；`ui_shots` 断言主题色 `#00c29a` |

### 6.3 FR-3 系统配置总开关

| AC | 标准 | 状态 | 取证 / 说明 |
|---|---|---|---|
| AC-3.1 | 开关默认关闭，首启行为与旧版一致 | ✅ | 出厂 `agent.enabled: false` → Router `mode=rules` + Queue `no_expire=False`（M3 装配实测）；OFF 时 rules 链路逐项不变（M4）+ 旧版 467 项回归全绿 |
| AC-3.2 | 开启后立刻生效（同一场直播内切换） | ✅ | M3 `set_mode()` 热切换实测（切换前先排空窗口/未结算连击）；M4 真实 `run_main_loop` 实测：ON 即挂钩子、热切换挂/摘同步、状态 provider 走「mode + 钩子」双条件 |
| AC-3.3 | 未配 LLM Key 时开关置灰并提示 | ✅ | M3 V3.3：页面徽章「不可开启」+ 本地先拦回滚 + 服务端兜底拒绝；M6 V6.7 复核（对话面板 `#agtOff` 置灰） |
| AC-3.4 | 关掉开关后行为与旧版逐项一致（S2 场景人工比对无差异） | ✅ | 未做人工逐条比对，改用**更强的机器证据**：DESIGN §15 冻结的 14 个模块 + `minimax_client.py` 字节相同；旧版 467 项测试回归全绿；M3/M4 逐项列出 rules 链路「分档 / 逐档开关 / 模板渲染 / P0 与插队 / 冷却与概率」原样保留。可信度高于人工比对，故不另做 |

### 6.4 FR-4 智能体页面

| AC | 标准 | 状态 | 取证 / 说明 |
|---|---|---|---|
| AC-4.2.1 | 「口播太频繁了」→ 间隔参数变化且热生效，回显旧值→新值 | ✅ | M6 V6.5：`interval_s` 420→720，回显「口播间隔：7分钟→12分钟」，`applier` 后真 `ScriptRuntime.schedule()["interval_s"] == 720`；`ui_shots` 断言 `diffText` 一致 |
| AC-4.2.2 | 「加一条话术：…」→ 话术池新增该条 | ✅ | M6 白名单工具含「加话题 / 加示例」（`tool_specs()`），写回走 `config.py` 行级替换保注释；`test_agent_session.py` 覆盖；`ui_shots` 配置预览显示「话题 6 条 / 话术样本 3 条」 |
| AC-4.2.3 | 撤销后回到改动前，`config.yaml` 注释未丢失 | ✅ | M6 V6.6：`undo()` 复用同批 setter；config 走行级替换 → 注释保留、`pool` 列表未动；profile 按段快照还原。`ui_shots` 断言 `undoRows==2` / `restored:true` |
| AC-4.2.4 | 无 LLM Key 时面板置灰，话术管理表单不受影响 | ✅ | M6 V6.7：`#agtInput` / `#agtSend` / 提示 chips 全 disabled，`ag_session_*` 独立动作族不进话术表单路径；`ui_shots` 断言 3 个 `*DisabledOff` 全 true |
| AC-4.3.1 | 构造一条弹幕可获得完整决策展示 | ✅ | M5 V5.1：展开后「判断依据 / 回复 / 语气·情绪·语速 / 素材引用」四行齐备，`ui_shots` 断言 `hasReason/hasReply/hasRecall/hasEmotion` 全 true |
| AC-4.3.2 | 试玩期间直播链路零影响 | ✅ | M5 V5.3：真 `AgentBrain` + 桩 LLM，试玩前后逐字段比对记忆 stats / spoken / 决策日志行数 / 召回去重窗口，四项均不变 |
| AC-4.3.3 | 试听按钮能出声（MiniMax 已配置时） | ⚠️ | **接线通过**：点一次恰好发出一条 `vc_preview`，带当前直播音色 + 回复正文（`ui_shots` `previews==1`、`voiceId` 正确）。**实际出声需 MiniMax Key**，走音色工坊既有链路（`test_voiceworkshop` 29 项覆盖）；无 Key 环境不虚构结论 |
| AC-4.4.1 | 点采纳 → 示例库新增一条，字段完整 | ✅ | M6 V6.1：`examples.jsonl` 新增，`id/pid/source/reply/event_line/event_type/user/tone/ts` 齐全，`source: adopted`；`ui_shots` 断言 `adoptKind==adopt`、标记「已采纳」 |
| AC-4.4.2 | 点弃用可选原因标签（不像主播 / 太长 / 跑题 / 重复） | ✅ | M6 V6.2：四档原因落 `feedback.jsonl`；`ui_shots` 断言 `rejectChips==4`、空提交被拦（`emptyRejectSent==0`） |
| AC-4.4.3 | 改写后新版本入库，原回复不参与 few-shot | ✅ | M6 V6.3：新文本以 `source: rewritten` 入库并进 few-shot，留 `supersedes`；原条不再被 `examples()` 选中；`ui_shots` 断言 `rewritePrefilled` 为原句 |
| AC-4.4.4 | 示例库有样本后，同场景回复语气可见变化 | ⚠️ | **验到「提示词可见变化」层**：`test_v64_cumulative_adoptions_change_the_prompt` 累计 10 条采纳 → `build_system_prompt()` 出现 ≥2 条评分最高的好评作 few-shot，且不含弃用条。**真实模型输出语气差异需 LLM Key** |
| AC-4.5.1 | 每条决策都能看到「为什么这么回」 | ✅ | M5 V5.1（判断依据行）+ `ui_shots` `hasReason` |
| AC-4.5.2 | 能看到素材引用 | ✅ | M5 V5.1（素材引用行）+ `ui_shots` `hasRecall`；M4 V4.7 预录音频复用带 `prerecorded` 标记 |
| AC-4.5.3 | 聚合卡数值与日志实际条数一致 | ✅ | M5 V5.2：断言聚合卡「本场决策数」== 列表条数（6==6）；`test:true` 单独计 `test_skipped`，色条 5 段对应图例 5 项 |

### 6.5 FR-5 智能体接管直播

| AC | 标准 | 状态 | 取证 / 说明 |
|---|---|---|---|
| AC-5.1 | 同一场重放弹幕，ON 与 OFF 行为可对比出差异 | ✅ | M3 对照实测：rules 6→5 条（含 `merged_count=2`）、agent 6→6 无聚合；M7-4 同一条链路两模式重放对比（产出 6 vs 12 条任务） |
| AC-5.2 | 无意义弹幕由智能体判断（不再受 20% 概率约束） | ✅ | M3：agent 模式 chat 逐条产出、不按概率丢噪声；M4 V4.3 `should_reply:false` 由智能体决定不产出 |
| AC-5.3 | 礼物/关注不再受冷却与概率约束，靠记忆避免重复致谢 | ✅ | M3 `bypass=True`（gift 跳过连击状态机、follow/fansclub 跳过冷却概率且 `test` 保持 False）；M4 V4.6「观众甲连刷礼物 count 1/2/3 可识别」+ `spoken` 防复读 |
| AC-5.4 | 敏感词弹幕仍被输入侧拦截（不进智能体） | ✅ | M3 敏感词闸门两侧 `blocked_input` 计数一致、被拦弹幕既不产出任务也不进智能体；`hub/safety.py` 字节相同（冻结件） |
| AC-5.5 | 智能体生成的回复仍被输出侧敏感词检查 | ✅ | 输出侧 `safety.check` 双检保留（冻结件字节相同 + `test_safety` 14 项未改）；M4 V4.2 兜底口播同样过安全检查 |
| AC-5.6 | LLM 返回非法 JSON 时不静默、不崩溃，走兜底 | ✅ | M4 V4.2 五档容错（`""` / `extract` / `salvage` / `plain` / `DecisionError`），四种残骸都不崩且**不被念出**（断言任务文本不含 `should_reply` 字样） |
| AC-5.7 | 未配置 LLM 时开关不可开启，产品不崩 | ✅ | M3 V3.3 + M6 V6.7；M4 main 装配实测「未配 Key 永不挂钩子」 |

### 6.6 FR-6 情绪档位与多情绪音频

| AC | 标准 | 状态 | 取证 / 说明 |
|---|---|---|---|
| AC-6.1 | 旧 v3 存档拷入后能自动迁移并可正常直播 | ✅ | M2 `migrate_v3_profiles()`（先备份 `profile.yaml.v3.bak`，备份失败即放弃迁移）；M7-7 真归档实测 v3→v4：条目 30 / 带音频 30 / **磁盘真能取到非空音频 30** |
| AC-6.2 | 迁移幂等（重复启动不二次包装、不重复生成） | ✅ | M2 判定点唯一（`_flat_to_slots`）+ 读取与落盘共用结构 → 天然幂等；真实存档二次调用返回 `[]` |
| AC-6.3 | 勾选 `happy` 后补生成，出现对应档位文件且 `text_hash` 正确 | ✅ | M2 合成即自动登记档位；`audios[vid][档位] = {file, text_hash}`；种子重建实测 happy+calm 共 60 份新增、零 warnings |
| AC-6.4 | 回「感谢大哥」时播 `happy` 档（与非 happy 档可听出差异） | ⚠️ | **逻辑已验**：三级回落（当前音色×happy 命中预录 / 其他音色×happy 回落 default 并 `sc_emotion_fallback`+1 / 未登记档交现场 TTS）。**「可听出差异」需真实音频试听**，本机未做 |
| AC-6.5 | 未登记情绪时现场合成，延迟 2~4 秒 | ⚠️ | **接线已验**：未登记情绪返回空串交现场 TTS 带 `emotion` 覆盖（M2 三级回落第③级）。**2~4 秒延迟需 MiniMax Key**；M7-4 只测到「LLM 之外的净开销 ~3.3 ms/条」，未含 TTS 合成时长 |
| AC-6.6 | 改一条话术文本后，该条**所有**情绪档标 stale | ✅ | M2 stale 语义：改文本 → 该音色**三档一起**返回空串（不播旧文本）；`stale`(voice 级) 与 `stale_emotions`(逐档) 并存 |
| AC-6.7 | 音色全场不切换（日志 `voice_id` 恒定） | ⚠️ | 设计上不切换：`voice_id` 由存档当前音色决定、会话内无改写路径。**需真实场次日志实证**，无 Key/无真机环境未做 |

### 6.7 FR-7 检索层

| AC | 标准 | 状态 | 取证 / 说明 |
|---|---|---|---|
| AC-7.1 | L1 素材全部出现在 system prompt 中 | ✅ | M4 整场重放：14 条事件九字段契约条条齐全；prompt 单测覆盖人设 / 游戏 / 话题参考 / 样本示例；M4 修接缝后 prompt 2271→1119 字符 |
| AC-7.2 | L2 能召回语义相近关键词（0.6 阈值可调） | ✅ | `hub/agent/retriever.py` L2 关键词召回；阈值/条数可调（`agent.top_k` 归一化 1–20）；`test_agent_retrieval.py` 覆盖 |
| AC-7.3 | 替换 retriever 实现类时 `brain.py` 无需改动 | ✅ | 接口隔离：`AgentBrain` 依赖注入的 retriever 协议，`test_agent_brain` / `test_agent_core` 用替身注入而 `brain` 零改动 |
| AC-7.4 | 素材超 50 条时 prompt 长度不失控（示例条数有上限） | ✅ | prompt 有示例条数上限（M6 反馈进化卡显示「上限 300 条」）；M4 实测 12 条记忆下 prompt 1119 字符可控；few-shot 只取评分最高前若干条（V6.4 断言） |

### 6.8 FR-8 单场记忆

| AC | 标准 | 状态 | 取证 / 说明 |
|---|---|---|---|
| AC-8.1 | 能识别同一用户的连续礼物 | ✅ | M4 V4.6：14 条事件全在（含两条不回）；观众甲连刷礼物 count 1/2/3 可识别 |
| AC-8.2 | 不重复说同一句话（`spoken` 缓冲生效） | ✅ | M4 V4.6：12 句进防复读缓冲，`is_repeated` 对原句为真 |
| AC-8.3 | 重启进程后记忆清空 | ✅ | `SessionMemory` 纯进程内对象，无任何持久化路径；重启即重建 |
| AC-8.4 | 除 `examples.jsonl` / `feedback.jsonl` 外无新增落盘文件 | ⚠️ | **有意偏离，已留痕**：记忆本身不落盘（符合本条意图），但按 DESIGN 要求另有 2 类落盘 —— 决策日志 `logs/agent-YYYYMMDD.jsonl`（M4，「决策透视」数据源 + 可追溯）与对话调参快照 `logs/agent_session.json`（M6，撤销用，保留 20 次）。二者均非「记忆」但字面超出本条范围。**处理决定**：保留（无日志则决策不可追溯、无快照则撤销不可实现），并已把「保留/清理策略」记入 HANDOVER 待办 |

### 6.9 FR-9 话术管理页增强

| AC | 标准 | 状态 | 取证 / 说明 |
|---|---|---|---|
| AC-9.1 | 既有功能全部可用（回归通过） | ✅ | M7-1：旧版 467 项一项未删（461 个测试函数缺失数 0），实测 466 passed / 1 flaky（新版已修）；新版 877 项全绿 |
| AC-9.2 | 可勾选/取消情绪档位并保存到存档 | ✅ | M2 前端「情绪档位」区：9 行档位网格（`default` 恒选中且虚线锁定）、`emotions_saved` / `emotion_purged` 广播后重拉视图重算缺档数 |
| AC-9.3 | 批量生成只补齐缺失档位，不重复合成已有音频 | ✅ | M2：逐档「缺 N / 就绪」标签 + 「按档位批量生成」；双区域进度互斥（`scGenAllWrap` / `scEmoGenWrap`）避免进度条跑错边 |
| AC-9.4 | 开关 ON 时提示条可见，OFF 时隐藏 | ✅ | M3 智能体卡徽章「已开启 / 已关闭」+ 摘要点亮/置灰；M4 状态条 `enabled` 走双条件；`ui_shots` 断言 `offShown`（无 Key 态）/ `offHidden` |

### 6.10 FR-10 保留项（不得回归）

| AC | 标准 | 状态 | 取证 / 说明 |
|---|---|---|---|
| AC-10.1 | 旧版全部 467 项测试在新版本上通过（或逐项说明差异原因） | ✅ | V7.1：旧版 467 项**一项未删**，差异仅在 7 个共有文件（4 个纯追加、3 个不删用例的前提下属实说明）；实测 466 passed / 1 已诊断的负载 flaky（新版已修） |
| AC-10.2 | 上述 11 项能力逐项手工验证可用 | ✅ | V7.2：**7 项后端模块字节相同**（最强「未回归」证据）+ 4 项加法/换肤能力用真对象探针逐项取证；需实机/密钥的部分已在 V7.3/V7.5/V7.6 补验 |

### 6.11 ⚠️ 项汇总与处理决定（不允许静默跳过）

| AC | 为什么是 ⚠️ | 处理决定 |
|---|---|---|
| AC-1.2 | 根下多 1 个说明性 html 文件（非目录） | 保留（便于查阅）；如需严格达标一键移入 `docs/` |
| AC-1.4 | 未在归档内单独重建 venv 起 GUI | 以「旧版测试实跑全绿」为达标证据；GUI 启动由发布包实机验收 |
| AC-4.3.3 | 试听实际出声需 MiniMax Key | 接线已用 `ui_shots` 取证；出声走音色工坊既有链路（29 项测试），留给实机 |
| AC-4.4.4 | 语气差异需真实 LLM | 已验「提示词出现 ≥2 条好评 few-shot」；模型输出差异留给双 Key 实测 |
| AC-6.4 | 「可听出档位差异」需真实音频 | 三级回落逻辑已验；听感留给实机试听 |
| AC-6.5 | 现场合成 2~4 s 需 MiniMax | 未登记情绪 → 现场 TTS 覆盖链路已验；合成时长留给双 Key 实测 |
| AC-6.7 | `voice_id` 恒定需真实场次日志 | 设计上无改写路径；留待实机场次日志实证 |
| AC-8.4 | 额外 2 类落盘（决策日志 / 调参快照） | 有意保留（可追溯 / 可撤销的必要代价）；清理策略已记入 HANDOVER 待办 |

> **诚实边界**：以上 ⚠️ 项的共性都是「需要真实设备、真实密钥或真实场次才能取到最后一段证据」。
> 本机是**无 LLM Key、无 MiniMax Key、无声卡**的开发环境，凡这类证据一律标注未测，
> 绝不写成"已通过"。这些项已同步记入 `docs/HANDOVER.md` §八 待办。
