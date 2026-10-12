# 仔仔鼠AI智能体直播助手 · 交接文档

> 新会话 AI 只读本文件 + `docs/DESIGN.md` 即可接手。
> 旧版全部历史文档已随旧版归档：`AIZB/_old-zaizaishu-live-20260921/`（其中 `HANDOVER.md` 是旧版交接文档）。

| 项 | 值 |
|---|---|
| 文档日期 | 2026-09-23 |
| 新版本目录 | `D:\AIZB\zaizaishu-agent-live` |
| 新版本状态 | **M0 ~ M7 全部完成，项目已可交付**：总开关 + 智能体接管 + 智能体页面 + 反馈进化 + 对话式调参 + 迁移脚本 + 旧版回归 + 性能实测 + 发布包组装（exe 19.0 MB）。**877 项测试全绿**；验收总表 54 条 AC：✅ 46 / ⚠️ 8（6 项需真实 Key/声卡，均已写明处理决定） |
| 新版本 git | `f2e5c93` → `e52a06e` → `e44c00a`（M0）→ `6423bb4`（M1）→ `31d658a` / `3fed89c` / `6180d65`（M2）→ `26fa998` / `30e58db` / `55c5967` / `b3b33cc` / `c34f4ce`（M3）→ `4afd890` / `53ff294` / `e099948` / `8cbc0f5` / `ed07970` / `85e5f1a`（M4）→ `96ea94a` / `e411bbd` / `c6be2cc`（M5）→ `2d37a38` … `a62af39`（M6）→ `0c7c151` / `547c903` / `525920d` / `b5dcb65` → 见 git log（M7-4 性能 + M7-6 文档） |
| 旧版归档 | `D:\AIZB\_old-zaizaishu-live-20260921` |
| 旧版基线 | v3 · git `fbaa64c` · 467 tests passed |
| 旧版发布包 | `…\_old-zaizaishu-live-20260921\zaizaishu-live\dist\release\`（已随 dist 排除，需重新打包） |

---

## 一、这是什么

旧版是一个可用的抖音直播 AI 自动互动工具：采集弹幕/礼物/关注 → LLM 生成口语回复 → MiniMax TTS → VB-Cable 虚拟声卡 → 直播伴侣当麦克风播出。主播人设「仔仔鼠」。单 exe 桌面应用（pywebview 壳），双击即用。

新版本在它之上**新增一个可开关的智能体**，并更新品牌视觉。核心是一句话：

> 加一个智能体页面和一个总开关。开关关掉，行为与旧版一字不差；开关打开，弹幕/礼物/关注/口播全部交给智能体自己判断怎么回。

### 三条不可妥协的前提

1. **不覆盖旧版**——旧版原样归档，两版独立 git 仓库
2. **音色继续 MiniMax**——不引入 GPT-SoVITS 或任何本地 TTS
3. **话术管理、音色工坊保留**——智能体在其之上加一层，不替代

> **作废路线提示**：归档目录内的 `仔仔鼠智能体版方案.md` 与 `智能体方案文档.md` 主张的是「GPT-SoVITS 替换 MiniMax + 智能体中心替换话术管理」。**该路线已作废**，不要照着它做。以 `docs/DESIGN.md` 为准。

---

## 二、路径与环境

### 2.1 目录

```
D:\AIZB\
├── .workbuddy\                        # 工作区记忆（不随归档移动）
├── _old-zaizaishu-live-20260921\      # 【归档】旧版全部内容，冻结，只读不改
│   └── zaizaishu-live\                #   旧版源码（git HEAD fbaa64c，工作区干净）
└── zaizaishu-agent-live\              # 【新版本】M4 完成，可直接开工 M5
    ├── .git/  .gitignore  .gitattributes
    ├── .venv/                         # 已建好（Python 3.14.7 + 依赖）
    ├── main.py  desktop.py  zaizaishu.spec
    ├── hub/                           # 核心模块（agent/ 待新增）
    ├── webui/index.html               # 控制台前端（4 标签页 → 待扩到 5）
    ├── assets/  tests/  scripts/  voice_clone/  replay/
    ├── config.yaml                    # 含 agent 段（不入 git）
    ├── config.example.yaml            # 含 agent 段（入 git）
    ├── logs/  profiles/               # 运行时数据（不入 git）
    └── docs\{DESIGN,PRD,IMPLEMENTATION,HANDOVER}.md
```

### 2.2 归档时排除了什么（重要）

| 排除项 | 原因 | 后果 |
|---|---|---|
| `.venv/` | venv 内部全为绝对路径，移动即失效 | **归档内的旧版跑不起来，需重建 venv** |
| `dist/` `build/` | 打包产物，已排除 | 旧版 exe 与发布包不在归档里，需重新打包 |
| `__pycache__/` `.pytest_cache/` | 缓存 | 无 |

共释放 518 MB，归档净体积 579 MB。

**搬动安全性已验证**：`hub/paths.py` 的路径锚点由 `__file__` 推导，`zaizaishu.spec` 用 `SPECPATH`，`config.yaml` 内的路径全为相对项目根的相对路径。归档后的旧版源码**不需要改任何路径**。

### 2.3 环境

- Python 3.14.7（`.venv`），必须用 `.\.venv\Scripts\python.exe`
- Windows 11 + PowerShell 5：不支持 `&&` / heredoc，多命令用 `;`
- git 提交用一次性环境变量 `GIT_AUTHOR_NAME/EMAIL` + `GIT_COMMITTER_NAME/EMAIL` = `zaizaishu` / `zaizaishu@local`，**勿改 git config**

---

## 三、怎么跑

### 3.1 归档里的旧版（需要时）

```powershell
cd D:\AIZB\_old-zaizaishu-live-20260921\zaizaishu-live
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest tests -q          # 应 467 passed
.\.venv\Scripts\python.exe main.py                     # 控制台 http://127.0.0.1:8766/
.\.venv\Scripts\python.exe desktop.py                  # 桌面窗口
```

### 3.2 新版本（M0 ~ M5 完成之后）

```powershell
cd D:\AIZB\zaizaishu-agent-live
# 首次：从归档派生源码基线（详见 IMPLEMENTATION.md §0.2）
.\.venv\Scripts\python.exe -m pytest tests -q          # 应 877 passed
.\.venv\Scripts\python.exe main.py
.\.venv\Scripts\python.exe desktop.py
.\.venv\Scripts\python.exe -m PyInstaller zaizaishu.spec --clean --noconfirm
```

**硬性节奏**：每完成一步 git 提交一次；任何改动后测试全绿再进下一步。

**前端改动后的三件套**（缺一不可，前两个是静态检查，第三个才碰真实渲染）：

```powershell
.\.venv\Scripts\python.exe scripts\emoji_scan.py                      # 零 emoji
.\.venv\Scripts\python.exe scripts\ui_audit.py                        # 配色/变量/对比度
.\.venv\Scripts\python.exe scripts\ui_shots.py --stub-ws --view 1366x768 --out logs/_shots/1366
.\.venv\Scripts\python.exe scripts\ui_shots.py --stub-ws --view 1920x1080 --out logs/_shots/1920
```

> `--stub-ws` 会在页面脚本之前装一个 WebSocket 替身喂夹具帧，让页面走**真实**消息分发与渲染，
> 并断言 V5.1/V5.2/V5.4 接线/V5.6；有 FAIL 退出码 1。**只跑前两个是不够的**——坑 47 就是这么漏掉的。

---

## 四、新旧版差异速查

| # | 维度 | 旧版 | 新版本 |
|---|---|---|---|
| 1 | 产品名 | 仔仔鼠AI直播助手 | 仔仔鼠AI智能体直播助手 |
| 2 | 主色调 | 蓝紫系 | 青绿系（主色 `#00C29A`，实心按钮 `#00a886`） |
| 3 | Logo | 蓝色版（`#4a93e8→#2f7de1`） | 绿色版（`#40d1b3→#009273`） |
| 4 | 图标 | 部分用 emoji（✓ ✗ ⚠ 💡 ✏️） | **全部内联 SVG**（`--ico-check/cross/warn/tip/arrow`），前端零 emoji |
| 5 | 标签页 | 4 个 | 5 个（新增「智能体」） |
| 6 | 系统配置 | 无智能体项 | 新增「智能体」卡片（总开关 + 运行时状态 + 档位摘要，**M3 已完成**）；未配 LLM Key 时置灰并说明原因 |
| 7 | 直播回复决策 | 三级应答（关键词直出 → 选样本 → 现场生成） | OFF 时不变；ON 时智能体自主决策（**M4 已完成**：五类事件逐条交 `hub/agent/`，回九字段 JSON 契约，命中召回素材时直接播预录音频） |
| 8 | 护栏 | 聚合/连击/冷却/概率/队列过期/让位全生效 | ON 时**除敏感词双检外全部放开**（M3 已可切换：Router 双模式 + Queue `no_expire`；M4 起逐类事件开关与礼物分档开关仍在智能体之前生效） |
| 9 | LLM 配置 | `llm` 段一套 | **复用同一套**，不新增 Key（决策用的 `hub/agent/llm.py` 只是把 `max_tokens` 提到 400 以容纳 JSON 契约） |
| 10 | TTS | MiniMax 流式 | **完全不变** |
| 11 | 配音音色 | 存档 `current_voice` | **不变**（不按句切换） |
| 12 | 配音情绪 | 全局固定 | 播放链路已支持逐条情绪（M2）：命中档位预录 / 已登记档缺音频回落 default / 未登记档现场 TTS；**谁来选情绪 M4 已补齐**（智能体决策里的 `emotion` / `speed` 经三态校验后透传到播放侧） |
| 13 | 存档音频结构 | v3：单份/音色 | **v4**：多情绪档/音色，v3 自动迁移（M2 已完成） |
| 14 | 话术管理页 | 存档/样本/话题/人设编辑 | 保留全部 + 新增情绪档位配置（M2 已完成）：9 行档位网格 + 逐档缺失提示 + 按档位批量生成 + 条目级档位子芯片 |
| 15 | 音色工坊页 | Key/复刻/试听/拉取/删除 | **完全不变**（智能体页的「试听」直接复用它的 `vc_preview` 通道） |
| 16 | 直播中枢页 | 消息流 | 每条 AI 回复旁加三个反馈图标（对勾/叉/铅笔）→ **已完成（M6）**：采纳 / 弃用（原因标签）/ 改写三路采集，落 `examples.jsonl` / `feedback.jsonl` |
| 17 | 智能体页 | 无 | **全新**（M5 已完成）：状态条（接管色点 + 模型名 + 本场决策数 + 平均耗时，整条可点跳系统配置）+ 试玩调试（事件构造器 / 送入智能体 / 批量投递 / 试听）+ 决策透视（四张聚合卡 + 时间倒序可展开列表）+ **反馈进化区（M6）** + **对话调参（M6）** |
| 18 | 发布包形态 | exe + config + 声卡安装器 + 音色库 | **基本不变**，但 exe **不再内联种子存档**（M7：种子移到发布包 `assets/seed_profile.zip`，exe 19.0 MB，首跑自动落地） |

---

## 五、当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 前置 | 归档旧版 + 建新版本目录 + 四份设计文档 | **已完成**（2026-09-21） |
| M0 | 工程准备（派生源码基线、重建 venv、测试全绿） | **已完成**（2026-09-21，commit `e44c00a`） |
| M1 | 视觉与品牌改造（青绿、logo、更名、去 emoji） | **已完成**（2026-09-21） |
| M2 | 存档 v4 与情绪档位 | **已完成**（2026-09-22） |
| M3 | 总开关与模式切换 | **已完成**（2026-09-22） |
| M4 | 智能体核心 | **已完成**（2026-09-22，commit `8cbc0f5` / `ed07970`） |
| M5 | 智能体页面 | **已完成**（2026-09-22，commit `96ea94a` / `e411bbd`） |
| M6 | 反馈进化与对话调参 | **已完成**（2026-09-23，commit `2d37a38` → `a62af39`） |
| M7 | 收尾（迁移脚本、回归、性能、发布包） | **已完成**（2026-09-23） |

**全部里程碑已完成（M0 ~ M7）**。详细步骤与每步验证标准见 `docs/IMPLEMENTATION.md`。

### 5.1 接手时的当前状态

M0 ~ M7 全部完成，**项目已可交付**：

- `zaizaishu-agent-live/` 内已有完整源码 + 已装好依赖的 `.venv`
- `.venv\Scripts\python.exe -m pytest tests -q` → **877 passed**（跑测试务必带 `--basetemp=<全新目录>`，并在跑前 `rm -rf` 它，否则安全删除拦截会让每个用例 setup ERROR）
- 前端验收四件套全过：`scripts/emoji_scan.py`（0 emoji）+ `scripts/ui_audit.py`（51 定义 / 0 未定义 / 0 自引用，48 行对比度全 PASS，无蓝紫残留）+ `scripts/ui_shots.py --stub-ws --view 1366x768` 与 `--view 1920x1080`（两档均 `errs == []`、横向溢出 0px）
- `config.yaml` / `config.example.yaml` 已含 `agent` 段（`enabled: false`），注释完整
- `zaizaishu.spec` 的 exe 名已改 `zaizaishu-agent`
- 首跑种子存档已落地在 `profiles/p_c2820e66`：**启动时已自动从 v3 迁到 v4**（原件备份 `profile.yaml.v3.bak`），当前音色仅 `default` 档有音频（happy/calm 各缺 30 条，可在控制台「情绪档位」区一键补齐）
- `assets/seed_profile.zip` **已重建为 v4**（69.2 MB，含当前音色 `default`+`happy`+`calm` 三档）
- **前端已是青绿主题 + 新品牌名 + 零 emoji**，`:root` 50 个变量，色值零散落；对比度 26 项全过；话术管理页新增「情绪档位」区；系统配置页新增「智能体」卡
- 绿色图标 `assets/icon.ico` 已生成（`scripts/make_icon.py`，需 `requirements-dev.txt` 的 Pillow）
- **M3 开关 + M4 决策主体都已可用**：控制台「系统配置 → 智能体」勾选即热切换（写 `config.yaml` + 切 `Router.mode` / `Queue.no_expire` + 挂/摘 `AgentBrain` 接管钩子，无需重启）。默认 OFF；未配 LLM Key 时置灰并说明原因。**ON 时五个事件类型（弹幕/礼物/关注/粉丝团/进场）全部逐条交给 `hub/agent/` 决策**：LLM 回九字段 JSON（要不要回 / 优先级 / 插队 / 语气 / 情绪档 / 语速 / 理由 / 话术 / 记忆笔记），情绪与语速透传到播放侧，命中召回素材时直接播它的预录音频；每条决策落 `logs/agent-YYYYMMDD.jsonl`
- `hub/agent/` 共 8 个模块：`memory`（单场环形缓冲）/ `emotion`（情绪三态校验）/ `decide`（契约解析 + 五档容错）/ `retriever`（L2 2-gram 召回，L3 接口预留）/ `prompt`（五段式 system prompt）/ `llm`（决策专用轻客户端）/ `log`（决策日志 JSONL + `summarize` 聚合）/ `brain`（决策主流程：`handle` 走 `_decide`，`preview` 只算不写）/ `playground`（试玩调度器，第 9 个模块，M5 新增）
- **M5 完成**：控制台新增第 5 个页签「智能体」（插在话术管理与系统配置之间）——
  **① 状态条**（接管色点 + 模型名 + 本场决策数 + 平均耗时 + 本场记忆，整条可点跳系统配置）；
  **② 试玩调试**（事件构造器支持五类事件 / 送入智能体看完整决策 / 批量投递模拟弹幕风暴 / 用当前直播音色试听 / 勾选自动试听）；
  **③ 决策透视**（本场决策数·平均延迟·不回比例三张卡 + 各类事件占比色条图例卡 + 时间倒序可展开列表，每条含判断依据 / 回复 / 语气·情绪·语速 / 素材引用）；
  数据源 `logs/agent-YYYYMMDD.jsonl` 最近 500 条，动作族 `ag_logs` / `ag_playground`。
  **试玩与直播严格隔离**：`AgentBrain.preview()` 只算不写（记忆 / 去重窗口 / 决策日志 / 队列四样都不动），已用真 brain + 桩 LLM 逐字段实测
- **M6 完成**：反馈进化与对话调参——
  **① 反馈采集**（直播中枢每条 AI 回复旁三个内联 SVG 图标按钮：对勾 / 叉 / 铅笔）→ `hub/agent/feedback.py` 的 `FeedbackStore` 三路采集：采纳 / 弃用（原因标签：不像主播 / 太长 / 跑题 / 重复）/ 改写，落 `profiles/<pid>/agent/examples.jsonl`（`source`: `adopted` / `rewritten` / `rejected`）与 `feedback.jsonl`；
  **② 样本回流**：`adopted` + `rewritten`（最新 ≤300 条）进 few-shot，`rejected` **不进**——`AgentBrain` 现读通道后端拼进 `build_system_prompt()`；
  **③ 反馈进化区**：样本总数 / 本周新增 / 采纳率 / 原因标签分布色条（WCAG 1.4.11 达标配色，见坑 61）；
  **④ 对话式调参**：`hub/agent/session.py` 的 `SessionTuner`——LLM function calling 改 6 个字段（语气 / 回复字数上限 / 口播间隔 / 加话题 / 加示例 / 游戏介绍），`tool_specs()` **显式白名单**（护栏字段模型永远改不动），改动前自动快照 `logs/agent_session.json`（保留 20 次）+ 撤销（复用同一批 setter，profile 按段还原）+ 回显「字段：旧值 → 新值」；改完经 `applier` 回调热生效（`script_rt.reload()` + `_apply_script_runtime()`，不重启）
- git 历史：`f2e5c93`（设计文档）→ `e52a06e`（清理 emoji）→ `e44c00a`（M0）→ `6423bb4`（M1）→ `31d658a`（M2 后端）→ `3fed89c`（M2 前端 + v4 种子）→ `6180d65`（M2 文档）→ `26fa998`（M3-1 配置）→ `30e58db`（M3-2/3 队列与 Router）→ `55c5967`（M3-4/5 动作族与装配）→ `b3b33cc`（M3-6 前端卡）→ `c34f4ce`（M3 文档）→ `4afd890`（M4-1 内核）→ `53ff294`（M4-2 日志/LLM/检索/提示词）→ `e099948`（M4-3 AgentBrain）→ `8cbc0f5`（M4-4 接管接线）→ `ed07970`（M4-5 验收 + 接缝修复）→ `85e5f1a`（M4 文档）→ `96ea94a`（M5-1/2 试玩内核与聚合）→ `e411bbd`（M5-3 动作族与装配）→ `c6be2cc`（M5-4/5 前端页签与验收）→ `2d37a38`（M6-1 feedback 存储层）→ `4071c7a`（M6-2 样本回流）→ `77b52f1`（测试时序修复）→ `4dc855c`（M6-3 采集入口）→ `45f614b`（M6-4 反馈进化区 + 配色修正）→ `c2d1824`（M6-5 对话调参内核）→ `a62af39`（M6-6 对话调参页面 + 装配 + 验收）→ `0c7c151`（M7-1 迁移脚本）→ `547c903`（M7-2 旧版回归对齐）→ `525920d`（M7-3 FR-10 逐项回归）→ `b5dcb65`（M7-5 种子外置 + 发布包脚本）→ 见 git log（M7-4 性能实测 + M7-6 文档收尾）

**M7 已完成（收尾）**：`scripts/migrate_from_old.py`（旧存档迁移，只复制不改源 + 两级校验 + 真 `ScriptStore` 试加载）→ 旧版 467 项回归对齐（**一项未删**，实跑 466 passed / 1 已诊断 flaky，新版已修）→ PRD §FR-10 的 11 项保留能力逐项回归（7 项模块字节相同 + 4 项探针取证）→ 性能实测（exe **19.00 MB**，比旧版 53.61 MB 还小；冷启动 3.84 s 首跑 / **2.76 s 稳跑**（旧版 2.85 s）；端到端 ON 比 OFF 每条多 **~3.3 ms**，相对 2 s 预算三个数量级余量；稳定性用「加速 soak + 2 条真实进程有界 soak」三条证据）→ `scripts/make_release.py` 组装发布包（8 文件 / 89.10 MB，**不含 `profiles/`**）→ `docs/HANDOVER.md` 收尾。
关键结论：**种子存档必须留在 exe 之外**（`zaizaishu.spec` 不再 datapack `assets/seed_profile.zip`，改由发布包外置 + `hub/scriptstore.find_seed_zip()` 两级选址），否则 69.2 MB 的 v4 种子会把 exe 撑到 88 MB、必然违反 NFR-2。见 HANDOVER §6.8 坑 62~67。

**下一步（交付后）**：需要**有 LLM Key + MiniMax Key + 真实声卡**的机器，把 6 项「有条件通过」的 AC 收尾（AC-1.4 归档独立 venv 起 GUI / AC-4.3.3 试听出声 / AC-4.4.4 语气差异 / AC-6.4 情绪档听感 / AC-6.5 现场合成延迟 / AC-6.7 `voice_id` 恒定），并按 NFR-3 复测真实链路延迟、补一次完整 4 小时墙上稳定性。逐项清单见 `IMPLEMENTATION.md` §6.11 与本文档 §八 待办。

**有意留下的可选优化项（不阻塞交付）**：① DESIGN §7 提到的 `ag_replay_decision`（决策回放复现）**有意未做**——V6.4「重放同一场看语气变化」目前只在测试层可验（提示词确实变了），要真做需先冻结"当时的 system prompt + 事件流"快照；② `logs/agent_session.json` 只留 20 次快照、无按天清理（决策日志 `logs/agent-*.jsonl` 同样无 30 天清理）；③ `agent` 段仍无独立的 `mode` 配置项（`mode` 由 `agent.enabled` 推导）；④ `assets/seed_profile.zip`（69.2 MB）仍入 git 并随发布包分发——若还要再瘦身，可降低种子话术条数或只留部分情绪档。


想复看视觉效果：`logs/_ui_shots/*.png`（M1 的 4 页签 + 特写）、`logs/_m3_shots/*.png`（M3 智能体卡的 5 个场景：关闭 / 开启 / 关回 / 未配 Key 置灰 / 整页）、`logs/_m5_shots/<视口>/*.png`（M5 的 5 页签 + 智能体页在两档视口下的真实渲染），均 `.gitignore` 忽略，仅本地。

**三个可复用的验收脚本**（改前端后必须重跑；只跑前两个**不够**，见坑 47）：

```bash
# ① 配色与变量审计：CSS 变量完整性 + 对比度实测 + 蓝紫残留（有 FAIL 返回非 0）
.venv\Scripts\python.exe scripts\ui_audit.py          # 加 -v 看逐项明细

# ② emoji 扫描：按 Unicode 区间判定，比 grep 字面量可靠（真 emoji 返回非 0）
.venv\Scripts\python.exe scripts\emoji_scan.py        # 加 --all 连排版标点一起报

# ③ 截图 + 前端功能查验：无头 Edge + CDP，零额外依赖（不用 Playwright，复用本机 Edge）
.venv\Scripts\python.exe scripts\ui_shots.py          # 自起 8799 静态服务，截 5 页签
.venv\Scripts\python.exe scripts\ui_shots.py --url http://127.0.0.1:8766/   # 或截真跑的控制台

# ④ 智能体页 + 反馈/对话调参功能查验（V5.1/V5.2/V5.4 接线/V5.6 + M6 查验，有 FAIL 返回非 0）——两档视口都要跑
.venv\Scripts\python.exe scripts\ui_shots.py --stub-ws --view 1366x768  --out logs/_m6_shots/1366
.venv\Scripts\python.exe scripts\ui_shots.py --stub-ws --view 1920x1080 --out logs/_m6_shots/1920
```

> 该脚本会在两档视口下各截 10 张（5 页签 + 智能体页特写 + 反馈进化区 + 对话调参），并断言：智能体页决策透视四行齐备、聚合卡==列表条数、反馈采集按钮真实发出 `ag_feedback`、反馈进化区四项指标上屏、**对话调参**（初始渲染 / 点提示真实发送 / 撤销 / 无 Key 置灰 / 复原）、页内横向溢出 0、`window.__errs == []`。三处已知脆弱点已修（per-pid profile / 就绪轮询 / 守护线程服务器，见坑 58/59/60）。

> `--stub-ws` 在页面脚本**之前**装一个 WebSocket 替身喂夹具帧，页面照常 `new WebSocket` /
> `send` / `onmessage`，所以被验证的是**真实**的消息分发与渲染代码（而不是工具里另写一份
> DOM 拼装）。它还会收 `window.onerror` / `unhandledrejection` 并断言 `errs == []`——
> 坑 47 就是靠它抓出来的。

> 本机系统代理会拦截回环请求（`curl http://127.0.0.1:8766/` 会拿到 502）。脚本内已用 `ProxyHandler({})` 绕开；手工调试时记得 `curl --noproxy '*'`。

---

## 六、开工须知（坑清单）

### 6.1 旧版继承来的坑（务必遵守）

1. **编码**：`.md` 文件勿用 PowerShell 管道读写（UTF-8 会被按 GBK 二次编码损坏）。改中文用 Read/Edit 工具或 Python 显式 UTF-8。全链路打印走 `logger.safe_print`（emoji 防 GBK 崩溃）。
2. **config.yaml 写回**：必须用 `hub/config.py` 的行级替换（`_extract_line_comment` 区分值内 `#` 与真注释），**勿整文件重写**（会丢注释）。
3. **VB-Cable 音频**：同名 CABLE 设备有 MME/DirectSound/WASAPI 三条目，**仅 MME + 24000Hz 可用**。设备解析走 `webui.resolve_output_device`。配置设备不存在时回退系统默认输出并有红色提示（**勿改回静默**）。
4. **MiniMax**：音色 15 槽是**账户级**共享，同账户多 Key 不扩容；标准 TTS 与克隆音色**分别计费**；余额不足报 `1008`。Key 存 `voice_clone/voice_clone_data/config.json`。
5. **存档防覆盖护栏**：`scriptstore.save_profile` 默认 `_guard_existing_audio`——整档保存携带空 audios 不冲掉磁盘已有引用；显式删除走 `guard=False`。**勿移除**。
6. **exe 替换必须重启进程**：旧进程持有旧代码。强杀用 `taskkill /PID <pid> /T /F`。
7. **播放全异步**：`stream.write` 必须 `asyncio.to_thread`，同步调用会冻结事件循环导致心跳停发断连。
8. **LLM / MiniMax 可选化**：未配置不崩——无 LLM 只收弹幕不回复；无 MiniMax 播放降级为打印。页内保存即热生效。
9. **config.yaml 不入 git**；改 Key 时注意源码与 dist 两处可能分叉。

### 6.2 新版本特有的坑

10. **开关必须走同一段代码**：OFF 不是"另写一套 rules 分支"，而是"不启用 agent 模块"。验收标准是逐项一致，不是"看起来差不多"。
11. **护栏只加开关不删代码**：所有"放开"必须可逆（`Router.mode`、`Queue.no_expire`）。不要为了放开而删掉冷却/概率/过期逻辑。
12. **存档迁移必须幂等**：v3 → v4 的包装判定靠"是否含 `file` 字段"。重复启动不能二次包装。迁移前备份 `profile.yaml` 为 `profile.yaml.v3.bak`。
13. **`text_hash` 要逐档位校验**：多情绪下一条文本对应多个音频文件，改文本必须让**所有档位**一起标 stale，否则会播出旧文本。
14. **情绪只能在 8 枚举内**：`hub/config.TTS_EMOTIONS = (happy, sad, angry, fearful, disgusted, surprised, calm, fluent)`。超出范围回落 `default`。
15. **音色不按句切换**：`voice_id` 全场固定走存档 `current_voice`，只切情绪参数。按句切音色需反复重建 `TTSClient`，代价高。
16. **前端禁用 emoji**：一律走 `--ico-*` 的 **SVG data-URI + CSS `mask` + `background: currentColor`**（`content:""` 的 `::before`），图标自动跟随元素文字色，不依赖系统 emoji 字体、不引外部图标库（保持单 HTML 文件）。页面内联 SVG 用 `stroke="currentColor"`。`<option>` 承载不了 SVG，那里就纯文字。
17. **前端颜色全部走 CSS 变量**：不允许硬编码色值。M1 已把旧版散布的 30 处硬编码深色（`#0b7a48`/`#c0392b`/`#9a6b07`）收口为 `--*-text` 令牌，新增语义色时照此办理，不要再写裸色值。
18. **SVG presentation attribute 不支持 `var()`**：`stop-color="var(--x)"` / `stroke="var(--x)"` / `fill="var(--x)"` 会被浏览器**整条丢弃**（不报错、静默失效）。内联 SVG 里只能写字面量 hex；`:root` 变量只服务 CSS 声明（`stroke: var(--x)` 可以）。M1 首版替换脚本踩过这个坑，回退了 6 处。
19. **写 CSS 变量定义时警惕全局替换**：把字面色值批量换成 `var(--token)` 时，刚写进 `:root` 的**定义行本身也会被命中**，产生 `--green-text: var(--green-text);` 自引用。CSS 自定义属性自引用会整条失效且浏览器不报错。改完务必跑一次**自引用检测**（`--a: var(--a)` 模式扫描）。
20. **「看起来浅绿」≠「白字能读」**：品牌色 `#00C29A` 配白字只有 2.29:1。凡是承载白字的实心块，用 `--brand-solid #00a886`（3.02–3.92:1）；品牌色当文字用 `--brand-deep #00614d`（7.1:1）；当图标/描边用 `--brand-press #009273`（3.92:1）。**对比度一律脚本实测，不靠目视**（`logs/_m1_contrast.txt` 有完整记录）。
21. **归档目录只读**：新版本代码不得读写 `_old-zaizaishu-live-20260921/` 内任何文件。
22. **不加限流**：这是显式知情决策（见 `DESIGN.md` §6.1）。不要再"顺手"加回节流/频控。上下文窗口容量（记忆缓冲条数）不算限流，它是 token 成本问题。
23. **决策契约要用容错链**：LLM 返回非法 JSON 有三种常见形态（截断 / 混入说明文字 / 完全非 JSON），三种都要有测试。

### 6.3 M2 新增的坑

24. **`default` 是内部兜底档名，不是 MiniMax 情绪取值**：它绝不能透传给合成接口（`Player._make_stream` 里 `emotion == "default" → None`）。传过去会被 MiniMax 当非法情绪，或让"无情绪"的默认音色变成某种奇怪语气。
25. **`generate_audio` 必须"生成即登记档位"**：生成了某档音频却不把该档写进 `emotions`，播放侧会把它当"未登记档"走现场 TTS —— 白花钱、白等延迟，而预录音频永远不被采用。
26. **MiniMax 有每分钟调用上限（RPM）**：批量预生成连续快速调用到 ~42 次起会连续报 `rate limit exceeded(RPM)`（`status_code=1002`），**失败的调用不产生音频**。批量任务必须限速（1.6 s 间隔实测安全）并做成**可断点续跑**（按 key 记 manifest、跳过已有文件），否则一次限流就要从头再来。
27. **改文本 → 该音色所有档一起 stale**：`stale`（voice 级）与 `stale_emotions`（逐档明细）并存。前端重生成**单档**后只能清掉该档的 stale 标记，其余档仍须重生成。
28. **批量生成进度条有两个去处，不能共用元素**：存档音色区 → `scGenAllWrap`，情绪档位区 → `scEmoGenWrap`。同时只允许一批（`scStartGenAll` 会拦截抢跑），否则进度会渲染到另一个区域。
29. **验证前端要走真协议消息，不能靠 CDP 塞状态**：`webui/index.html` 的脚本整体包在 `(() => { ... })()` 里，`scCur` / `renderScEditor` 都**不在全局作用域**，`Runtime.evaluate` 拿不到。正确做法是起一个"同端口 HTTP 静态 + WS"的假后端（`websockets.process_request` 顺带伺服 `index.html`），按真实协议推 `{type:"script",data:{op:...}}`，这样连 `applySc` 分支一起验到。
30. **假后端的 `process_request` 必须放行 WS 升级请求**：无条件按静态文件处理会把 `/ws` 404 掉，页面永远停在「连接断开，重连中」，且**不报错**、看起来像前端坏了。判据 `Upgrade: websocket` 头 → 返回 `None`。

### 6.4 M3 新增的坑

31. **`agent.enabled` 与"运行时模式"要分两层报**：配置里开了但未配 LLM Key 时，运行时实际跑的是规则管道。状态里必须同时给出 `configured_enabled` / `downgraded` / `note`，否则页面开关显示 ON、实际跑 rules，用户会以为坏了。`mode` / `enabled` / `no_expire` 一律以**运行时**为准。
32. **热切换先落盘再切运行时**：`update_agent_config` 写失败就整个不动（返回失败原因），不要"先切内存再写文件"——否则会出现「这次生效、重启又回去」的假象。
33. **切换模式前必须排空旧模式的中间态**：聚合窗口、礼物连击表、冷却积压都是 rules 模式专用状态，agent 模式不认识它们。`set_mode()` 先 `_flush_intermediate()` 把它们一次性产出，再换模式（否则这些半截状态永远不产出）。
34. **`sec_idx` 要指向段头而不是种子键行**：`lines.append("agent:")` + `lines.append("  enabled: false")` 之后取 `len(lines) - 1` 会指到 `enabled` 那行，导致后续写回把新键插到段外、出现重复 `enabled:`。正解 `len(lines) - 2`。
35. **枚举值写回不要一律加双引号**：`update_llm_config` 的 `_yaml_quote` 是给含特殊字符的密钥用的；沿用它会把 `layer: l2` 写成 `layer: "l2"`，与模板不符且每次开关都产生 diff。`update_agent_config` 走 `_yaml_scalar()`：布尔小写、数字裸写、字符串只在必要时加引号。
36. **同层同名键靠缩进精确相等区分**：`agent.enabled`（2 空格）与 `agent.emotion.enabled`（4 空格）同名不同层。行级写回必须按缩进定位（`_block_bounds` 要求缩进**精确相等**），用"包含即可"的匹配会改错行。
37. **同步钩子抛异常不能落进"没配 LLM 就静默"的分支**：`_dispatch_context` 里钩子异常若回落到"走 reply_builder"，而 agent 模式下 `reply_builder` 可能是 `None` → 异常被吞成静默。正解：钩子异常与异步生成器异常一致地走 `_fallback_reply()`（`safety.fallback_reply` 兜底口播），并抽出 `_fallback_reply()` 给两条路径共用。
38. **`StatsCollector.get()` 只接受一个参数**：它不是 `dict.get`，写 `stats.get("dropped_stale", 0)` 会 `TypeError`。要拿字典口径用 `stats.summary().get(k, 0)`。
39. **`grid-auto-rows: 1fr` 下卡片数为奇数会留空单元格**：系统配置页卡片数从 6 变 7 后尾行右侧出现空白（看着像渲染 bug）。给新卡加 `grid-column: 1 / -1` 占满整行，顺带用两栏内布局（`repeat(auto-fit, minmax(300px, 1fr))`）消化宽度。
40. **卡片级"结果提示"会被每秒快照冲掉**：快照帧与动作回推走同一个 `applyAgent`，若不区分就会每条快照清一次说明。用「状态指纹」判定：只在状态真变化时清提示，`op === "ag_saved"` 的帧无条件覆写（服务端在 data 里带 `op` / `ok` / `message`，顶层另留 `ok` 供按钮复位）。
41. **想让页面执行一个前端动作，只能由服务端广播驱动**：页面脚本在 IIFE 里，`send` 不在全局（第 29 条的延伸）。假后端要维护 `CLIENTS` 连接表 + `broadcast()`，脚本另开一条 WS 发自定义动作，服务端改状态后广播快照给页面。注意 `async for raw in ws` 的 handler 只在**该连接**上回消息，不显式广播就改不动别的客户端。

### 6.5 M4 新增的坑

42. **接缝测试要喂"真产物"，不能喂"理想形状"**（M4 最值钱的一条）。`SessionMemory.brief()` 返的是事件 **dict**，而 `prompt._memory_section()` 是纯文本装配器（`f"· {行}"`）——于是 system prompt 里出现 `· {'type': 'chat', 'user': '观众甲', 'gift_value': 0, 'ts': 1790049318.18}` 这样的 Python repr。12 条记忆时 system prompt **2271 字符 → 修复后 1119 字符（-103%）**，且**每个事件**都要付一次；模型还得从 dict repr 里读信息。原提示词单测传的是手搓的字符串列表，所以一路全绿。**规则：凡是"某个对象被另一个模块渲染"的地方，测试必须从真对象取产物**（这里是从真 `SessionMemory` 取 `brief()`），并断言渲染结果里不出现 repr 残片（`'type':`、`'gift_value':`、裸时间戳浮点）。
43. **推广一个钩子时，别漏掉同步分支的调用点**：把 `_emit_chat_result` 合并成 `_emit_agent_result` 时，漏改了 `_finalize_chat_context` 里同步生成器的那个调用点（异步分支改了），17 个用例立刻 `AttributeError: 'Router' object has no attribute '_emit_chat_result'`。改名/合并出站函数后，用 `grep -n "<旧函数名>"` 全域确认归零，别只改"看起来最像"的那一处。
44. **`max_reply_chars` 不能在构造期缓存**：`main` 里 `AgentBrain` 可能建在存档装载完成**之前**，构造期读会锁到 `config.persona` 的兜底值；现读还顺带让主播改字数上限后下一次决策即生效。同类"读存档/读配置"的值，凡是可能早于装载时机构造的，一律现读。
45. **决策类的 LLM 调用不能用默认 `max_tokens`**：`llm` 段默认 120，而九字段中文 JSON 契约（含 reason / reply_text / memory_note）必然截断 → 截断后的 JSON 若照"降级为纯文本"处理，就会把 `{"should_reply": true, "reply_` **念给观众听**。`hub/agent/llm.py` 用 `DECISION_MAX_TOKENS = 400`，并给容错链加了一档 `salvage`（正则抢救 `reply_text` / `should_reply`）+ 残骸判定（抢救不出就抛错走兜底口播，绝不念残骸）。
46. **`interrupt` 只在 `priority == 0` 时有效，要在两处都夹一次**：`decide.py` 归一化时夹（防模型乱给），`router._emit_agent_result` 落地时再夹（防别的调用方绕过 decide 直接给 dict）。只写一处就会被绕过。同理 `_AGENT_DEFAULT_PRIORITY` 让"决策没给 priority"时按事件类型兜底（礼物 P0、其余 P1），而不是一律 P1。

### 6.6 M5 新增的坑

47. **页面脚本是 IIFE，内部函数从外部一律拿不到**（`<script> (() => { ... })()`）。所以前端功能验收**不能**靠"调内部函数注入数据"。两条正路：① 走真实消息路径（在页面脚本**之前**装一个 WebSocket 替身喂帧，或对着真后端跑）；② 点真实 DOM 元素驱动。在验收工具里另写一份 DOM 拼装 = 验工具不验产品。**推论：静态检查（配色 / emoji）全过 + 截图正常，都不代表功能可用**——见下一条。
48. **`msg.type` 的分发点最容易写错，而且只在运行时报出来**：「决策透视」那条分发写成了 `applyAgLogs(...)`，函数实际叫 `agpApplyLogs` → `ReferenceError` → **决策透视永久空白，M5 最关键的功能静默失效**。而 `ui_audit`（配色）、`emoji_scan`（图标）、空态截图三个验收面**都看不见**。修法：命名前缀统一（`agp*`），并让验收工具收 `window.onerror` + `unhandledrejection`，把 `errs == []` 当成硬断言。
49. **CDP `Runtime.evaluate` 在页面抛异常时只回 `subtype:"error"`、没有 `value`**：不显式检查就会静默拿到 `None`，"注入失败"看起来像"注入生效但没数据"。而 `setTimeout` / 事件回调里抛的异常**连这个都不会有**——必须在页面里自收 `window.onerror`。
50. **夹具（fake 记录）必须与生产对象同形状**：给决策透视喂的假记录缺 `emotion` / `speed` 时，页面渲染成 `情绪：— → —`，看着像页面 bug，实际是夹具没喂真形状。**「喂真产物」这条规矩对夹具同样适用**（坑 42 的姊妹条）。已加断言 `hasEmotion`（同时查 `— → —` 不出现）守住。
51. **页面里的数字带单位时，断言要先剥单位**：聚合卡的 `textContent` 是 `6 条` / `33.3 %`，直接与 `6` 比较会"看起来不一致"。用 `replace(/[^0-9.]/g, '')` 再比。
52. **快照帧会把整页状态填满，不能污染其它页签的截图**：M5 的 V5.4 试听查验需要 `snapshot.config.voice_id`（页面从快照取"当前直播音色"），所以放在**所有截图之后**跑，而不是顺手加在智能体页那一步。
53. **自定义底色的文字要顺手补进对比度审计**：M5 新增了 `--surface-2` 底上的一批文字（试玩结果卡 / 聚合卡 / 决策列表），而 `ui_audit.build_rows()` 是**静态枚举**的——不补行就不会被验。补的三行：`--ink` 14.86:1、`--ink-2` 5.77:1、`--ink-3` 3.08:1，均 PASS（`--ink-3` 贴着 3.0 门槛，别再往浅调）。

### 6.7 M6 新增的坑

54. **`config.yaml` 里的 `schedule.interval_min` 在建成存档后是"死值"，改它不会热生效**（M6-5 最值钱的一条）。它在全仓只有两个读者：`Scheduler.__init__`（**构造期默认值**）与 `main._migrate_legacy_pool`（**一次性迁移**）。一旦 profile 里已有话题，`_apply_script_runtime()` 每次都用 `ScriptRuntime.schedule()` → `profile.schedule.interval_s` 把调度器重新配一遍。所以「说一句『口播太频繁了』只写 config.yaml」会得到一个**看起来执行了、实际没变**的假效果——正是 V6.5 要防的东西。**正解：两处都写**——profile 副本是活值（热生效），config 副本与 DESIGN §7.2① 对齐、兼作"还没有存档"时的引导值。**规则：要把一个配置项做成"可对话调参"，先 `grep -n "<key>"` 找到它的**全部**读者，判定哪个读者在运行期被调用；只改没人读的那个 = 假效果。** 同理 `max_reply_chars` 的唯一权威是 `profile.knowledge.max_reply_chars`（`AgentBrain._resolve_max_reply_chars()` 每次决策现读），`config.yaml persona.max_reply_chars` 只是兜底。
55. **注入的假时钟会让"毫秒时间戳 ID"撞号**：快照 id 用 `ss_<ms>`，`SessionTuner` 支持 `clock` 注入（单测必然冻结时钟），于是"撤销回滚到指定快照"的用例里 id 重复。修法是生成 id 时加唯一性循环（撞了就 `_1`、`_2`）。**凡是"时间戳当 ID"又允许注入时钟的地方，都要有撞号兜底**——真实机器上毫秒级并发也会偶发。
56. **撤销要复用与正写**同一批 setter**，且 profile 只回滚"被碰过的段"**：撤销若自己再算一遍（比如"旧分钟数 × 60"）就会在"原值是 `interval_s: 90`"这种非整分钟时把值改坏。正解：profile 侧的撤销按**段快照**还原（`restore["profile"]["sections"]`），只写回本次真正动过的那几段，既不会算错，也不会覆盖会话期间别处（如话术管理页）并发保存的改动；config 侧才按"旧值重写"。同时 `tool_specs()` 必须给 LLM 一个**显式白名单**——**没进白名单的字段（敏感词 / F9 暂停 / 播放串行 / `event_enabled`）模型就永远改不动**，护栏不靠"提示词请求它别改"。
57. **Git Bash heredoc 写文件会双重执行 + 把 `\n` 变成 `/n`**：`cat >> tests/test_config.py <<'PYEOF' … PYEOF` 在本机被应用了**两遍**（函数定义重复），且字符串字面量 `"app:\n"` 被路径转换改写成 `"app:/n"`（MSYS 把 `\n` 当路径分隔处理）。**所有要写进仓库的文件一律用 Write/Edit 工具**，别用 shell 重定向；`.md` 更是如此（管道/重定向会把 UTF-8 二次编码成 GBK，中文全毁）。
58. **无头浏览器的 `--user-data-dir` 必须按进程隔离 + 退出时清理**：`ui_shots.py` 两次背靠背运行时，第二次会**交接给上一次正在退出的 Edge 实例**（同一个固定 profile 目录），表现为"页面停在 `loading|0|0` 三十秒"。修法：profile 目录带 `os.getpid()`、`finally` 里 `shutil.rmtree`，并在检测到卡住时**重新导航一次**。
59. **截图前必须先 `scrollIntoView({block:'center'})`**：M6 新增的「对话调参」卡在首屏之下，不滚动就拍到页面顶部——**截图"有内容"不等于"拍到了目标"**。M5 的「反馈进化区」也踩过同一条。凡卡片位置可能在折叠线以下的，截图前一律滚到居中。
60. **`ThreadingTCPServer` 默认 `daemon_threads=False` 会让进程退出时挂死**：`ui_shots.py` 的 WS 替身在收尾时，若有一个请求线程正阻塞在 socket 写，`server_close()` 会等它 → **脚本打印完结果却永不退出**（后台任务挂十几分钟）。修法：`daemon_threads=True` + `block_on_close=False` + 显式 `srv.server_close()`。
61. **图表里"用颜色把色条段和图示项对应起来"属于 WCAG 1.4.11 的"图形"，门槛是 3:1，不是"装饰"的 1.0**：M6-4 的原因分布色条（8px 段）+ 图示色块（9px），颜色是段与标签之间的**唯一联系**，所以每一档都必须对卡片底色 ≥3:1。首版用了 `--brand-primary`（2.11:1）/ `--gold`（1.87:1）/ `--ink-3`（2.90:1）——**目视"挺鲜艳"但全部不达标**。改成 `--brand-press` 3.62 / `--gold-text` 4.95 / `--red-text` 5.02 / `--ink-2` 5.77 才过。**规矩没变（坑 53）：新加了自定义底色的文字/图形，必须顺手补 `ui_audit.build_rows()` 的静态行**，否则配色坏了审计也不报。

### 6.8 M7 新增的坑

62. **大而少变的资源（种子存档）绝不能和代码一起塞进 onefile exe**：`assets/seed_profile.zip` 从 33.8 MB（v3 单档）涨到 69.2 MB（v4 三档）后，内联进 exe 让新版变成 **92.5 MB**（旧版 53.61 MB，NFR-2 上限 58.61 MB）——而且 onefile 每次启动都要把这 69 MB 解包到临时目录，冷启动也被拖慢。正解是**发布包外置**：spec 不再 datapack 种子，`hub/scriptstore.find_seed_zip()` 做两级选址（包内 `BUNDLE_DIR/assets/` → exe 旁 `EXE_DIR/assets/`）。改完 exe **19.0 MB**，稳态冷启动还比旧版**快 3%**。判据：`build/zaizaishu/Analysis-00.toc` 里按条目大小聚合一眼就能看出谁在吃体积。
63. **测 exe 启动前必须同时改掉 `webui.listen` 与 `danmaku.ws_listen` 两个端口**：只改前者时，`ws_listen` 若被别的实例占着，采集器绑定失败会让**中枢线程早退**——现象是"种子已落地（说明进程活过）但控制台永远不就绪"，极易误判成"exe 起不来"。另外启动子进程时要清掉 `http_proxy/https_proxy` 并设 `NO_PROXY=*`，否则轮询自己的回环端口会拿到代理的 502。
64. **`Router(random=...)` 要的是「模块式对象」，不是 callable**：内部走 `self._random.random()`（follow / fansclub 的冷却概率），传 `lambda: 0.0` 会在运行时炸 `AttributeError: 'function' object has no attribute 'random'`。要确定性就传 `random` 模块或带 `.random()` 的替身。
65. **重放文件首行是直播间信息（无 `timestamp`）、且事件 ts 用的是实时钟**：解析要跳过首行；`ReplaySource` 是 `normalize(dy, ts=self._clock())` 打的 ts，所以"产出时刻 − `task.event_ts`"**就是**该条的处理耗时，不必自己按 `speed` 折算模拟偏移（算错还会反向放大）。记录产出时刻要用 `time.time()`，**别用 `perf_counter`**——两个时基不可混算。
66. **`pywebview` 不在 `requirements.txt` 里（历史遗留）**：`desktop.py`（exe 入口）import 它，打包前必须装，否则 exe 一起来就 ImportError。已补进 `requirements-dev.txt` 并注明原因；只跑源码 `main.py`（控制台路线）不需要它。同理 Bash 里别用裸 `python`——本机可能落到系统 Python 而不是 `.venv`，一律用 venv 的绝对路径。
67. **"4 小时稳定性"要诚实地降级取证，并写明是有界实测**：墙上 4 小时跑不了，就用两条互补证据——① **加速 soak**（真 `Router` + 真 `PrioritySpeakQueue` + 真 `AgentBrain` + 真决策日志，按事件量压出内存趋势）；② **真实进程有界 soak**（发布包 exe 挂 `app.replay_file` 跑满整场，周期采样 RSS + 控制台存活）。文档里**必须**写成"有界实测 + 依据"，不能把缩短版说成达标——这和 M6 里"V6.4 只验到提示词层"是同一条纪律。

---

## 七、文件速查（新版本，M5 之后）

| 文件 | 职责 | 状态 |
|---|---|---|
| `main.py` | 入口装配 + 开关热生效（读 `agent_settings` 决定 Router/Queue 模式、写盘后切运行时）+ **`_make_brain` / `_attach_agent`（M4 注入 AgentBrain）+ LLM 热改后 `rebind_llm`** + v2/v3 存档迁移 | **M3+M4 已完成** |
| `hub/router.py` | 事件路由；`mode`（rules / agent）+ `set_mode()` 热切换 + `set_agent_handler()` + **`_agent_active()`（mode 与钩子双条件）+ 五类事件统一出站 `_emit_agent_result`（承接 priority/interrupt/emotion/speed/audio_path）** | **M3+M4 已完成** |
| `hub/queue.py` | 优先级队列；`no_expire` + `set_no_expire()`（`_is_stale` 恒 False、`put_idle` 不查积压） | **M3 已完成** |
| `hub/config.py` | `agent_settings` / `agent_available` / `agent_status` / `update_agent_config`（行级写回）+ `agent` 段校验 | **M3 已完成** |
| `hub/scriptstore.py` | 存档 v4：二维 audios + `emotions` 档位集合 + v3 幂等迁移（含 `.v3.bak` 备份）+ `generate_audio` 合成注入 | **M2 已完成** |
| `hub/selector.py` | `_resolve_audio` 三级回落（请求档 → default → 空串）+ `current_emotions()` + **公开入口 `resolve_audio(item, emotion)`（供检索层标 `has_audio` 与预录音频复用）** | **M2+M4 已完成** |
| `hub/events.py` | `SpeakTask` 加 `emotion` / `speed` | **M2 已完成** |
| `hub/player.py` | `_make_stream`：非 default 档现场 TTS 带 emotion（记 `emotion_onsite`），default 档绝不透传档位名 | **M2 已完成** |
| `hub/tts.py` | `synthesize_stream` 支持逐条 emotion/speed/vol/pitch 覆盖（None = 沿用构造期配置） | **M2 已完成** |
| `hub/webui.py` | 存档动作族 + 合成通道注入 + `ag_status` / `ag_set_enabled`（含 `agent_status_provider` / `agent_switcher` 注入点）+ 快照 `agent` 段（M4 起含 `model` / `session`）+ **M5 动作族 `ag_logs` / `ag_playground`（含 `agent_logs_provider` / `agent_playground` 两个注入点，`limit` 夹 1~2000）** | **M2+M3+M4+M5 已完成** |
| `hub/agent/brain.py` | 事件 → 决策主流程；**M5 拆成 `_evaluate()`（纯计算）+ `_decide()`（写记忆/去重/日志）+ `preview()`（只算不写，试玩零影响可实测）**；`handle()` / `reset_session()` / `rebind_llm()` | **M4+M5 已完成** |
| `hub/agent/memory.py` | 单场环形缓冲（不落盘）；`brief()` 返**渲染好的人话行**（见坑 42） | **M4 已完成** |
| `hub/agent/retriever.py` | 检索接口 + L2 实现（预留 L3）；标 `has_audio`、去重窗口、`entry_item()` | **M4 已完成** |
| `hub/agent/prompt.py` | system prompt 五段组装（底料 / 召回 / 记忆 / 情绪档位 / JSON 契约） | **M4 已完成** |
| `hub/agent/decide.py` | 决策契约校验与**五档容错**（`""` / `extract` / `salvage` / `plain` / 抛 `DecisionError`） | **M4 已完成** |
| `hub/agent/emotion.py` | 情绪三态校验（档位内 / 8 枚举内 / 非法回落 default）与语速夹取 | **M4 已完成** |
| `hub/agent/log.py` | 决策日志 JSONL（`logs/agent-YYYYMMDD.jsonl`，按天滚动，写失败只 warn）+ **`summarize()`（决策透视四张聚合卡口径；`test: true` 不计入本场决策）** | **M4+M5 已完成** |
| `hub/agent/llm.py` | 决策专用 LLM 客户端（`max_tokens=400`、超时下限 12 s、失败重试 1 次） | **M4 已完成** |
| `hub/agent/playground.py` | **试玩调试调度器**（`Playground` / `build_context` / `build_batch` / `summarize`）；五类事件校验、字段裁剪、一次最多 20 条、整批先校验、单条失败不中断 | **M5 已完成** |
| `hub/agent/feedback.py` | 反馈采集与示例库：`FeedbackStore`（`adopt` / `reject`（原因标签）/ `rewrite`）→ `profiles/<pid>/agent/examples.jsonl`（`source`: `adopted` / `rewritten` / `rejected`）+ `feedback.jsonl`；`examples()` = `adopted`+`rewritten` 最新 ≤ `MAX_EXAMPLES=300`（`rejected` **不进** few-shot）；`stats()`（总数 / 本周新增 / 采纳率 / 原因分布） | **M6 已完成** |
| `hub/agent/session.py` | 对话式调参：`SessionTuner`（`tool_specs()` 白名单 6 个 function calling 工具）+ `_resolve` 契约 → `_apply`（profile + config，**多 op 共用一个快照**）+ 写前快照 `logs/agent_session.json`（`MAX_SNAPSHOTS=20`）+ `undo()`（复用同一批 setter / profile 按段还原）+ `applier` 热生效回调 | **M6 已完成** |
| `hub/agent/__init__.py` | 包导出（`AgentBrain` / `AgentDecision` / `DecisionError` / `SessionMemory`） | **M4 已完成** |
| `tests/test_agent_e2e.py` | M4-5 端到端验收：main 装配接线 + 整场重放（V4.1~V4.7） | **M4 已完成** |
| `tests/test_agent_playground.py` | M5-1/2：试玩隔离（V5.3 逐字段实测）+ 事件构造器 + 批量上限 + 聚合口径（V5.2） | **M5 已完成**（31 项） |
| `tests/test_webui_agent_page.py` | M5-3：`ag_logs` / `ag_playground` 动作族 + 真 WS 往返 | **M5 已完成**（18 项） |
| `tests/test_agent_feedback.py` | M6-1/2/3：三路采集、双 file 落盘、few-shot 过滤（`rejected` 不进）、回流注入 | **M6 已完成** |
| `tests/test_agent_feedback_reflow.py` | M6-2/4：采纳样本回流进 `build_system_prompt()`；**累计 10 条好评后提示词可见变化**（V6.4 的可测层） | **M6 已完成** |
| `tests/test_agent_session.py` | M6-5：`SessionTuner` 白名单/上限、六个工具各自的**落点**、多 op 共用一个快照、noop、撤销语义、快照上限与消费，以及**接缝**（真 `ScriptRuntime.reload()` → `schedule()["interval_s"]` / `max_reply_chars()` / `build_system_prompt()`） | **M6 已完成**（40 项） |
| `tests/test_agent_session_wiring.py` | M6-6：真 `SessionTuner` 挂 `CollectingWebUi` 跑三个动作 + 失败路径 + `test_actions_are_in_dispatch_table` + 真 `run_main_loop` 装配（含无 Key 降级） | **M6 已完成**（13 项） |
| `webui/index.html` | 青绿主题 + 新品牌名 + 零 emoji（`:root` 变量 / `--ico-*` 图标库）+ 话术管理页「情绪档位」区 + 系统配置页「智能体」卡 + M5 第 5 页签「智能体」（状态条 / 试玩调试 / 决策透视，`agp-*` 样式族）+ **M6 直播中枢三图标反馈按钮 + 弹窗 / 「反馈进化」区 / 第 5 页签『对话调参』（左对话 + 右配置预览，`agt-*` 样式族）** | **M1+M2+M3+M5+M6 已完成** |
| `assets/icon.ico` | 绿色版图标（`#40d1b3→#009273`，7 种尺寸） | **M1 已完成** |
| `scripts/make_icon.py` | 图标生成（构建期，需 `requirements-dev.txt` 的 Pillow） | **M1 已完成** |
| `scripts/ui_shots.py` | 无头 Edge + CDP 截图；M5 加 `--view WxH`（V5.6 两档）与 `--stub-ws`（WS 替身喂夹具帧 → 走真实渲染）；**M6 加第三组查验「对话调参」（初始渲染 / 点提示真实发送 / 撤销 / V6.7 置灰 / 复原）+ per-pid profile + `wait_ready()` 就绪轮询 + 守护线程服务器（见坑 58/59/60）** | **M1+M5+M6 已完成** |
| `scripts/ui_audit.py` | 配色 / 变量 / 对比度审计；M5 补 `--surface-2` 底三行；**M6 补 13 行（反馈按钮 / `.fb-mark` / 图表色块与色条段）+ 5 行（对话调参气泡与快照行），共 48 行全 PASS** | **M1+M5+M6 已完成** |
| `requirements-dev.txt` | 构建期依赖：Pillow（图标）+ **PyInstaller**（打包）+ **pywebview**（`desktop.py` 运行时依赖，历史遗留未在 requirements.txt 声明） | **M1 新增，M7 扩充** |
| `assets/seed_profile.zip` | 种子存档 v4：121 entries / 69.2 MB，当前音色 `default`+`happy`+`calm` 三档齐全 | **M2 已完成** |
| `scripts/migrate_from_old.py` | 旧存档迁移（M7）：从归档目录**只复制** `profiles/` 与 `voice_clone/voice_clone_data/`；两级校验（引用音频缺失=硬错误 / 音色库查不到=告警，音色取源∪目标并集）；`--dry-run` / `--force` / `--skip-*` / `--verify`（用真 `ScriptStore` 试加载触发 v3→v4 并逐条目标定磁盘音频）；退出码 0/1/2 | **M7 已完成**（42 项测试） |
| `scripts/make_release.py` | 发布包组装（M7）：组装 `dist/release/<产品名>/`（exe 改名 + config + blockwords + 虚拟声卡两件 + **外置种子** + 音色库）；不带 `profiles/`；`--with-uploads` / `--dry-run` | **M7 已完成** |
| `tests/test_migrate_from_old.py` | M7-1：纯函数 / inspect 各分支 / 源目录不变性（大小+mtime_ns 快照）/ 跳过与覆盖 / dry-run 不落盘 / 退出码 / **接缝**（真 `ScriptStore` 加载迁移产物） | **M7 已完成**（42 项） |
| `tests/test_make_release.py` | M7-5：发布包布局 / 不带 profiles / uploads 可选 / dry-run / 缺素材报错 / 退出码 + **种子外置接缝**（包内优先→exe 旁兜底→都没有；外置种子真能解出存档）+ 回归「spec 不许内联种子」 | **M7 已完成**（14 项） |
| **冻结不改** | `minimax_client.py` `voiceworkshop.py` `safety.py` `generator.py` `prompt.py` `templates.py` `douyin.py` `dysign.py` `dyproto.py` `replay.py` `hotkey.py` `scheduler.py` `watchdog.py` `stats.py` `logger.py` | — |
| **M1 已改，后续冻结** | `desktop.py`（仅 `TITLE` + docstring 产品名）、`zaizaishu.spec`（docstring）、`scripts/make_icon.py`、`scripts/tts_stream_check.py`（注释） | 已改 |
| `zaizaishu.spec`（**M7 例外**） | DESIGN §15 只许改 name/icon，但 M7 额外从 `datas` **移除 `assets/seed_profile.zip`**（69.2 MB 内联把 exe 撑到 92.5 MB，超 NFR-2）→ 种子改由发布包外置。这是一处**有意偏离冻结清单**的改动，已在 IMPLEMENTATION.md「M7 实测记录」里写明理由与替代验证 | **M7 已改（有记录）** |

---

## 八、待办

### 8.1 本次范围内的待办

见 `docs/IMPLEMENTATION.md` 的 M0 ~ M7。**当前进度：M0 ~ M7 全部完成，可交付**。

M7 已交付：`scripts/migrate_from_old.py`（旧存档迁移）+ `scripts/make_release.py`（发布包组装）+ 旧版 467 项回归对齐 + PRD §FR-10 11 项保留能力逐项回归 + 性能/稳定性实测 + 发布包组装。详见 `IMPLEMENTATION.md` 的「M7 实测记录」。

M7 期间的**两处需要知晓的决定**：
1. **种子存档改为发布包外置**（`zaizaishu.spec` 从 `datas` 移除 `assets/seed_profile.zip`）：69.2 MB 的 v4 种子内联把 exe 撑到 92.5 MB，超 NFR-2 上限（旧版 53.61 MB + 5 MB）。这是**有意偏离 DESIGN §15 冻结清单**（spec 原只许改 name/icon）的一处改动，已在 `IMPLEMENTATION.md` 写明理由与替代验证；`hub/scriptstore.find_seed_zip()` 两级选址（包内 → exe 旁）保证首跑照旧自动落地。
2. **`pywebview` 未在 `requirements.txt` 声明**（历史遗留，旧版同样没有）→ 已补进 `requirements-dev.txt`；打包前必须装，否则 exe 起不来。

M7 阶段抓到的真实缺陷与取证手法见 `HANDOVER.md §6.8 M7 新增的坑`（坑 62~67）。

### 8.2 范围外但值得记着

| # | 事项 | 来源 |
|---|---|---|
| 1 | **批量决策**：一次 LLM 调用处理多条事件，省 token、降延迟 | 本次无限流决策的后续优化方向（`PRD.md` OQ-3） |
| 2 | **L3 语义向量 RAG**：素材超 100 条后启用，接口已预留 | `DESIGN.md` §9 |
| 3 | **决策日志保留与清理**：建议 30 天自动清理 | `PRD.md` OQ-4 |
| 4 | **token 消耗统计**：取决于 LLM 接口是否返回 | `PRD.md` OQ-5 |
| 5 | 删除 `ws_server.py` 外挂兜底（8765）与 Dycast 残留 | 旧版待办 |
| 6 | 企业交付：离线一机一码授权（Ed25519）→ PyArmor 混淆 → 在线激活 | 旧版待办，详见归档内 `HANDOVER-old-20260916.md` |
| 7 | 参数化预生成音频（旧版 `synthesize_wav` 预生成用默认参数） | **M2 已顺带解决**：`generate_audio(pid, item_id, voice_id, emotion)` 按「音色 × 情绪档」逐档传参 |
| 8 | 种子存档体积（M2 后 69.2 MB，随包 + 入 git） | **M7 已解决**：种子移出 exe（发布会包外置 `assets/seed_profile.zip`），exe 19.0 MB；`seed_profile.zip` 仍入 git（69.2 MB）。**若还想再减**：降低种子话术条数或只留部分情绪档（本次未做） |
| 9 | **`agent.mode` 无独立配置项**：`mode` 完全由 `agent.enabled` 推导（`enabled: true` → agent）。若将来要"只放开部分护栏"（例如 agent 决策但保留队列过期丢弃），需要给 `agent` 段加一个独立开关 | M3 落地时的口径决定（见 `IMPLEMENTATION.md` M3 实现补充第 1 条） |
| 10 | **`logs/agent_session.json` 只保留最近 20 次快照，没有按天/大小清理**；决策日志（`logs/agent-YYYYMMDD.jsonl`）同样只按天滚动、无 30 天清理 | M6-5 落地（与第 3 条同源，建议发布后统一加清理策略） |
| 11 | **`ag_replay_decision`（决策回放复现）有意未实现**：DESIGN §7 提及的"回放同一场看语气变化"目前只在测试层可验（V6.4 = 提示词可见变化），页面没有对应按钮；要真做需先冻结"当时的 system prompt + 事件流"快照 | M6 验收时的口径决定 |
| 12 | **端到端"弹幕→出声"的真实延迟未测**（LLM + MiniMax 双 Key 缺失）：M7 只实测了非 LLM 的管线净开销（ON 比 OFF 多 ~3 ms/条，见 M7 实测记录），真实链路延迟需在有 Key 的机器上按 NFR-3 复测 | M7 实测时的口径 |
| 13 | **4 小时稳定性为「有界实测 + 依据」，非完整 4 小时**（墙上时间不允许）：加速 soak 与真实进程有界 soak 两条证据已落在 M7 实测记录 | M7 实测时的口径 |

---

## 九、一句话交接

新版本目录里是**从旧版 v3 派生的完整可运行源码**（旧版原样冻结在 `../_old-zaizaishu-live-20260921/`，HEAD `fbaa64c`、工作区干净）。**M0 ~ M7 全部完成，项目已可交付**：M0（工程准备）、M1（青绿视觉 + 更名 + 去 emoji）、M2（存档 v4 + 情绪档位）、M3（总开关与模式切换）、M4（智能体核心，接管决策）、M5（智能体页面）、M6（反馈进化 + 对话式调参）、M7（迁移脚本 + 旧版回归 + FR-10 逐项回归 + 性能实测 + 发布包 + 文档收尾）。**877 项测试全绿**，`IMPLEMENTATION.md` §六 验收总表逐项对照 PRD 的 54 条 AC：**✅ 46 / ⚠️ 8**（8 项中有 6 项是"需真实设备 / 密钥 / 场次"，1 项是根目录结构、1 项是有意保留的额外落盘，全部写明原因与处理决定，无静默跳过）。**发布包已可组装**：`python scripts/make_release.py` → `dist/release/`（exe 19.0 MB，比旧版还小）。接手时先读 `docs/DESIGN.md`（尤其 §五 接管机制、§六 护栏处置、§七 前端设计、§八 音色与情绪），再按 `docs/IMPLEMENTATION.md` 的「验收总表 + 待办」把需要真实 Key / 声卡的 6 项 ⚠️ 收尾。

**改前端必跑**：`scripts/ui_audit.py` + `scripts/emoji_scan.py` + `scripts/ui_shots.py --stub-ws --view 1366x768|1920x1080`。前两个是静态检查，**只跑它们会漏掉"功能静默失效"**（见坑 47/48）。

一句话概括这个项目要做的事：**给「仔仔鼠AI直播助手」装一个可开关的智能体大脑** —— 开关 OFF 时行为与旧版逐项一致；开关 ON 时，智能体绕过旧版的聚合/冷却/概率/过期/让位等硬性护栏（敏感词双检保留），自己判断哪些弹幕值得回、用什么语气回、现场生成口播内容，并带单场短期记忆。上游采集与下游 TTS/播放完全复用，MiniMax 引擎与话术管理、音色工坊两个页面都保留。
