# 仔仔鼠AI智能体直播助手

在旧版「仔仔鼠AI直播助手」v3 基础上升级的新版本：新增一个**可开关的智能体大脑**，让直播间回复从「规则 + 话术库」升级为「智能体自主判断」，同时把视觉从蓝紫改为青绿、正式更名。

> 当前状态：**M0、M1 已完成** —— 源码基线已从旧版派生、venv 已建、467 项测试全绿；前端已是青绿主题 + 新品牌名 + 零 emoji，绿色图标已生成。下一步 **M2（存档 v4 与情绪档位）**。

---

## 一句话理解本次改动

> 加一个智能体页面和一个总开关。开关关掉，行为与旧版一字不差；开关打开，弹幕/礼物/关注/口播全部交给智能体自己判断怎么回。

---

## 三条不可妥协的前提

| # | 前提 |
|---|---|
| 1 | **不覆盖旧版**——旧版全部内容原样归档在 `../_old-zaizaishu-live-20260921/`，两版独立 git 仓库 |
| 2 | **音色继续 MiniMax**——不引入 GPT-SoVITS 或任何本地 TTS 引擎 |
| 3 | **话术管理、音色工坊保留**——智能体在其之上加一层，不替代它们 |

---

## 文档导航

| 文档 | 内容 | 什么时候读 |
|---|---|---|
| [`docs/DESIGN.md`](docs/DESIGN.md) | 技术设计：架构、接管机制、护栏处置、音色与情绪、检索层、记忆、数据、前端、配置 | 想搞清"怎么做"，或对某个设计决策有疑问 |
| [`docs/PRD.md`](docs/PRD.md) | 产品需求：背景、场景、12 组功能需求与验收标准、非功能需求、优先级 | 想确认"做什么"与"怎么算做完" |
| [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) | 实施计划：M0~M7 分阶段步骤、每步验证标准、测试策略、发布包组装 | 准备动手写代码 |
| [`docs/HANDOVER.md`](docs/HANDOVER.md) | 交接：路径环境、怎么跑、差异速查、进度、坑清单、待办 | 新会话接手，或隔一段时间回来 |

---

## 本次改动的关键决策（速览）

| 议题 | 决策 |
|---|---|
| 智能体生效方式 | 系统配置里的总开关，**默认关闭**；OFF 时走旧版同一段代码 |
| 接管程度 | 开关 ON = **完全接管**（弹幕/礼物/关注/口播全部自主判断） |
| 护栏 | **除敏感词双检外全部放开**，不加任何限流（显式知情决策，见 `DESIGN.md` §6.1） |
| 话术存档定位 | 作为智能体的**素材库**：人设/知识继续喂 system prompt，样本/话题降级为参考素材，音色继续走存档 `current_voice` |
| 智能体 LLM | **复用现有 `llm` 段配置**，不新增 Key |
| 检索层 | L1 素材直注 + L2 关键词粗筛；**L3 语义向量 RAG 预留接口**，素材超百条后再做 |
| 记忆 | **仅单场内存**（环形缓冲），进程重启即清空 |
| 音色 | 全场固定，**不按句切换** |
| 情绪 | 由智能体**每轮决定**（emotion / speed），预录音频提供多情绪档 |
| 存档结构 | v3 → **v4**（`audios` 多情绪档），旧存档自动迁移、幂等 |
| 情绪档位 | 出厂默认 `happy` + `calm`，话术管理页可勾选；未登记情绪走现场 TTS |
| 视觉 | 蓝紫 → 青绿（主色 `#00C29A`，实心按钮 `#00a886`）；**前端禁用 emoji**，一律 SVG mask 图标（`--ico-*`） |
| 对比度 | 26 项关键组合全部达标；品牌色承载白字的固有上限已在 `DESIGN.md` §13.4 说明 |
| 智能体页面 | 四个功能区：对话调参 / 试玩调试 / 反馈进化 / 决策透视 |

---

## 目录结构

```
zaizaishu-agent-live/
├── README.md  .gitignore  .gitattributes
├── .venv/                   # Python 3.14.7 + 依赖（已就绪）
├── main.py  desktop.py  zaizaishu.spec
├── hub/                     # 核心模块
│   └── agent/               # 智能体（M4 新增）
├── webui/index.html         # 控制台前端（4 标签页 → M5 扩到 5）；M1 已青绿化 + 去 emoji
├── assets/                  # 图标（M1 已换绿）+ 种子存档（M2 重建 v4）
├── requirements-dev.txt     # 构建期依赖（Pillow，仅生成图标用）
├── tests/  scripts/  replay/  voice_clone/
├── config.yaml              # 含 agent 段，不入 git
├── config.example.yaml      # 含 agent 段，入 git
├── logs/  profiles/         # 运行时数据，不入 git
└── docs/                    # 四份文档
```

---

## 与旧版的关系

| 项 | 位置 |
|---|---|
| 旧版归档 | `../_old-zaizaishu-live-20260921/`（579 MB，冻结只读） |
| 旧版源码 | `../_old-zaizaishu-live-20260921/zaizaishu-live/`（git `fbaa64c`） |
| 旧版交接文档 | `../_old-zaizaishu-live-20260921/HANDOVER.md` |

**版本隔离原则**：新版本不 import、不读写归档目录内任何文件；两版不共享 config / profiles / 音色库 / 日志。需要在两版之间搬运的只有旧版的存档与音色库，走一次性迁移脚本（`scripts/migrate_from_old.py`，M7 落地）。

> **作废路线提示**：归档内的 `仔仔鼠智能体版方案.md` 与 `智能体方案文档.md` 主张的是「GPT-SoVITS 替换 MiniMax + 智能体中心替换话术管理」路线，**该路线已作废**，以本目录 `docs/DESIGN.md` 为准。

---

## 关于本仓库（脱敏公开版）

本仓库是从私有开发仓库导出的**脱敏公开版**，已做如下处理：

| 处理 | 说明 |
|---|---|
| 移除密钥 | `config.yaml`、`voice_clone/voice_clone_data/config.json` 等含真实 API Key 的文件**均不在本仓库**；首次运行由程序自动生成空白模板，密钥请在控制台内自行填写 |
| 移除个人数据 | 真实主播的话术存档 `profiles/`、克隆音色样本与上传录音 `voice_clone/voice_clone_data/`、运行日志 `logs/`、真实录制 `replay/` 均未纳入 |
| 移除发布素材 | `assets/seed_profile.zip`（出厂种子存档，含**真人音色样本**）与 `assets/vbcable/`（第三方虚拟声卡安装包）已剔除 |
| 替换个人标识 | 文档/测试中的真实姓名、音色 ID、本机绝对路径已替换为中性占位值 |

### 首次运行需要自备的两样东西

1. **出厂种子存档 `assets/seed_profile.zip`（可选）**
   程序首跑会在 `assets/seed_profile.zip` 找不到时**自动降级**为一份空的「默认话术」存档，不会崩溃——你可以直接在控制台「话术管理」页里自建存档。
   如需内置一份出厂存档：在控制台建好存档后，用「导出存档」得到 zip，放到 `assets/seed_profile.zip` 即可（首跑自动落地）。

2. **虚拟声卡 VB-CABLE（直播出声必需）**
   用于把 AI 语音送进直播伴侣的麦克风。请到 VB-Audio 官方站点自行下载安装：
   <https://vb-audio.com/Cable/>（Windows 版免费）。安装后按 `RUNBOOK.md` 第 1 节把 `config.yaml` 的 `audio.device` 指向 `CABLE Input`。

### 快速上手

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py          # 或 python desktop.py 弹原生窗口
```

启动后浏览器打开 <http://127.0.0.1:8766/>，在「系统配置」页填 LLM Key、「音色工坊」页填 MiniMax Key（保存即热生效）。详见 [`RUNBOOK.md`](RUNBOOK.md)。

> **许可**：本仓库暂未附带 LICENSE 文件（默认「保留所有权利」）；如需开源授权，请自行添加（如 MIT / Apache-2.0）。
>
> **第三方声明**：VB-CABLE 为 VB-Audio 公司的独立第三方软件，版权归其所有，本仓库不附带、不重新分发。
