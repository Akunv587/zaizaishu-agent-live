"""智能体的 LLM 薄客户端（DESIGN §4.2「LLM（复用 config.llm）」）。

**为什么不复用 `hub.generator.Generator`**：

1. `hub/generator.py` 在 DESIGN §15 的冻结清单里，一行不改；
2. `Generator` 的 `system_prompt` 是**有状态**的（rules 链路 `set_system_prompt` 热替换），
   而智能体每次决策的 system prompt 都不同（记忆与召回随事件变化），借它的状态会打架；
3. `Generator._request()` 会做 `clean_reply` 话术清洗（剥引号、按字数截断），
   而决策调用要的是**原始 JSON 文本**，清洗会破坏结构。

共用的是**同一套 `llm` 配置**（base_url / api_key / model），不新增 Key——这点与
DESIGN §12.3 一致。

**决策调用的额度为什么要单独给**：`llm.max_tokens` 默认 120，装不下"9 个字段 + 一段
中文口播"的 JSON（中文约 1 token/字），必然截断——正好落进 `decide` 的 salvage 档，
等于白丢一半决策质量。故决策调用用 `DECISION_MAX_TOKENS=400`，超时下限 12 s
（system prompt 带人设 + 召回 + 记忆，比 rules 链路那条短提示词重得多）。
这两项**当前是模块常量**，若后续要按用户可调，应落进 `agent.llm.*` 的配置段（M7 评估）。

**两个入口**（M6 加的第二个）：

| 入口 | 返回 | 用途 |
|---|---|---|
| `ask(system, user)` | 文本（`content`） | 决策调用：要的就是那段 JSON 文本 |
| `ask_message(system, user, tools=…)` | `choices[0].message` 原样 | 对话调参的 function calling：要同时看 `tool_calls` 与 `content` |

两者共用同一段 `_chat`（重试次数、额度、统计键完全一致），差别只在
「带 tools 时允许 content 为空」——模型只回工具调用不带寒暄是合法的。
"""
import time

import httpx

from hub.logger import get_logger

# 决策调用的额度（见模块 docstring 的说明）
DECISION_MAX_TOKENS = 400
DECISION_TIMEOUT_FLOOR_S = 12.0
MAX_ATTEMPTS = 2   # 与 rules 链路一致：原样重试 1 次


class AgentLLMError(Exception):
    """决策请求失败（网络 / 非 200 / 结构不符 / 空响应）——由 brain 走兜底口播。"""


class AgentLLM:
    """`async (system, user) -> 原始文本`；不解析、不清洗，解析交给 `decide`。"""

    def __init__(self, config: dict = None, log=None, stats=None,
                 clock=None, client_factory=None,
                 max_tokens: int = DECISION_MAX_TOKENS):
        cfg = config or {}
        llm = cfg.get("llm", {}) or {}
        base_url = str(llm.get("base_url", "")).rstrip("/")
        self._url = f"{base_url}/chat/completions"
        self._api_key = str(llm.get("api_key", ""))
        self._model = str(llm.get("model", ""))
        self._temperature = float(llm.get("temperature", 0.8))
        self._max_tokens = int(max_tokens)
        self._timeout_s = max(float(llm.get("timeout_seconds", 8)),
                              DECISION_TIMEOUT_FLOOR_S)
        self._stats = stats
        self._log = log or get_logger("agent.llm")
        self._clock = clock or time.time

        def _default_factory():
            return httpx.AsyncClient(timeout=self._timeout_s)

        self._client_factory = client_factory or _default_factory

    @property
    def model(self) -> str:
        return self._model

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._url and self._model)

    async def ask(self, system: str, user: str) -> str:
        """发一次决策请求，返回模型原始文本；连续失败抛 `AgentLLMError`。"""
        msg = await self._chat(system, user)
        return str(msg.get("content"))

    async def ask_message(self, system: str, user: str, tools=None,
                          tool_choice: str = "auto", max_tokens: int = None) -> dict:
        """function calling 用：返回 `choices[0].message` **原样**（含 `tool_calls`）。

        与 `ask` 的差别只有两点（M6 对话调参需要）：

        1. 带 `tools` 时**允许 `content` 为空** —— 模型只回工具调用、不带寒暄是合法的，
           按 `ask` 的口径会被当"空响应"重试，白烧两次额度；
        2. 不做任何取值/清洗，调用方自己决定怎么解析（会话层要同时看
           `tool_calls` 与 `content` 两条路径）。

        重试次数、额度、统计键与 `ask` **完全一致**（同一段 `_chat` 代码）。
        """
        return await self._chat(system, user, tools=tools, tool_choice=tool_choice,
                                max_tokens=max_tokens,
                                require_content=not tools)

    async def _chat(self, system: str, user: str, tools=None, tool_choice: str = "auto",
                    max_tokens: int = None, require_content: bool = True) -> dict:
        """一次带重试的对话请求，返回 `choices[0].message`（dict）。"""
        if not self.configured:
            raise AgentLLMError("LLM 未配置（base_url / api_key / model 不全）")
        payload = {
            "model": self._model,
            "temperature": self._temperature,
            "max_tokens": int(max_tokens) if max_tokens else self._max_tokens,
            "messages": [{"role": "system", "content": str(system or "")},
                         {"role": "user", "content": str(user or "")}],
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        prompt_chars = len(str(system or "")) + len(str(user or ""))

        started = self._clock()
        last = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                msg = await self._post(payload, headers)
                if require_content and not str(msg.get("content") or "").strip():
                    # 与重构前一致：空响应也算失败，照样重试一次
                    raise AgentLLMError("响应文本为空")
                elapsed = self._clock() - started
                if self._stats is not None:
                    self._stats.inc("agent_llm_ok")
                self._log.info("agent_llm_answered", {
                    "attempt": attempt, "elapsed_s": round(elapsed, 2),
                    "prompt_chars": prompt_chars,
                    "answer_chars": len(str(msg.get("content") or "")),
                    "tool_calls": len(msg.get("tool_calls") or [])})
                return msg
            except Exception as e:  # 网络 / HTTP / 结构 / 空响应一律原样重试
                last = f"{type(e).__name__}: {e}"
                if self._stats is not None:
                    self._stats.inc("agent_llm_fail")
                self._log.warning("agent_llm_failed", {
                    "attempt": attempt, "reason": last, "prompt_chars": prompt_chars})
        raise AgentLLMError(last or "决策请求连续失败")

    async def _post(self, payload: dict, headers: dict) -> dict:
        """POST 一次并返回 `choices[0].message`（原样，含 tool_calls）。"""
        async with self._client_factory() as client:
            resp = await client.post(self._url, json=payload, headers=headers)
        self._log.debug("agent_llm_request", {"url": self._url, "payload": payload})
        status = getattr(resp, "status_code", 0)
        if status != 200:
            raise AgentLLMError(f"HTTP {status}")
        try:
            body = resp.json()
            msg = body["choices"][0]["message"]
        except (TypeError, KeyError, IndexError, ValueError) as e:
            raise AgentLLMError(f"choices 解析失败: {e}") from e
        if not isinstance(msg, dict):
            raise AgentLLMError(f"choices[0].message 不是对象：{type(msg).__name__}")
        return msg
