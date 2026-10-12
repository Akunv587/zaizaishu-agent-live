"""智能体模块（M4，DESIGN §4.2/§4.3）。

职责边界：本包只负责**应答决策**——「这条事件值不值得回、用什么语气回、说什么」。
上游采集/事件与下游队列/安全/合成/播放全部复用既有模块，本包不碰。

内部模块：

| 模块 | 职责 |
|---|---|
| `memory.py` | 单场记忆（环形缓冲，进程内，不落盘） |
| `retriever.py` | 检索层接口 + L2 关键词召回实现 |
| `prompt.py` | system prompt 组装（人设 + 素材 + 记忆 + 输出契约） |
| `decide.py` | 决策契约校验与三级容错 |
| `emotion.py` | 情绪档位校验与回落、语速夹取 |
| `llm.py` | LLM 薄客户端（复用 config.llm，不碰冻结的 hub.generator） |
| `log.py` | 决策日志 JSONL |
| `brain.py` | 决策主流程（Router 的接管入口） |

**导入方向**：本包可 import hub.config / hub.scriptstore / hub.prompt / hub.logger；
对 `hub.router` 只在运行时惰性引用（`memory._default_similarity`），
避免与 router 形成模块级循环——router 侧只通过 `set_agent_handler()` 接收一个
可调用对象，两边的耦合仅此一处。
"""
from hub.agent.brain import AgentBrain
from hub.agent.decide import AgentDecision, DecisionError
from hub.agent.memory import SessionMemory

__all__ = ["AgentBrain", "AgentDecision", "DecisionError", "SessionMemory"]
