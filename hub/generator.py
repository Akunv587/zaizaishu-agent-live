"""LLM 话术生成客户端（PROMPTS 步骤 3.1/3.2，DESIGN §4.3/§7.2；PRD FR-1/FR-7）。

OpenAI 兼容 Chat Completions：POST {base_url}/chat/completions（正斜杠拼接）。
system 提示词由 hub.prompt.build_system_prompt 渲染（人设模板），输出经
clean_reply 清洗（剥引号、删 emoji、超长截断）。
降级链：超时、非 200、choices 解析失败、清洗后为空四种情况原样重试 1 次，
仍失败返回 safety.fallback_reply；每次失败对 stats 的 llm_fail 计数，
成功对 reply_generated 计数。
留痕：每次请求的完整 messages 与响应原文写 debug 级日志（提示词调教的数据原料）。

select_reply（v3.1 template 方案）：同一客户端的「选择题」形态——从存档候选
参考回复里选一条应答弹幕（温度 0、只输出编号，0=都不合适），请求连续失败
抛 LLMError 由调用方降级（静默）；选中计 llm_select。
"""
import re
import time

import httpx

from hub.logger import get_logger, safe_print
from hub.prompt import build_system_prompt, build_user_message, clean_reply


class LLMError(Exception):
    pass


# template 方案应答选择：只输出编号的确定性选择（温度 0、小 max_tokens）
# 注意：推理模型（如 deepseek-v4-flash）会把 token 花在内部推理上，16 会返回空，
# 故给足 64 让其腾出作答空间（A+B：调大 + 提示词强约束）。
SELECT_MAX_TOKENS = 64
SELECT_SYSTEM_PROMPT = (
    "你是直播弹幕的应答选择器。给你一条观众弹幕和一组候选回复，每条候选带"
    "「适用」场景与「回复」内容。任务：若某条候选的适用场景与观众弹幕语义高度一致、"
    "其回复能恰当回应这条弹幕，输出该候选的编号；若没有任何候选合适，输出 0。"
    "直接输出一个数字，不要思考、不要解释、不要复述、不要任何其他内容。"
)


def _parse_selection(text: str) -> int | None:
    """从 LLM 输出提取候选编号；0=都不合适；解析不出数字返回 None（按未选中）。"""
    m = re.search(r"\d+", str(text or ""))
    return int(m.group()) if m else None


async def probe_llm(base_url: str, api_key: str, model: str,
                    timeout_s: float = 10.0, client_factory=None) -> str:
    """连通性验证（控制台保存 LLM 配置前的「测试连接」）。

    发一条 max_tokens=1 的最小 chat 请求：成功返回空串，失败返回可读原因
    （网络错误 / HTTP 状态码 / 响应非 OpenAI 兼容格式）。client_factory 供测试注入。
    """
    url = f"{str(base_url).rstrip('/')}/chat/completions"
    payload = {"model": model, "max_tokens": 1,
               "messages": [{"role": "user", "content": "hi"}]}
    headers = {"Authorization": f"Bearer {api_key}"}

    factory = client_factory or (lambda: httpx.AsyncClient(timeout=timeout_s))
    try:
        async with factory() as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        return f"网络错误: {type(e).__name__}: {e}"
    status = getattr(resp, "status_code", 0)
    if status != 200:
        detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                err = body.get("error")
                detail = str((err or {}).get("message", "") if isinstance(err, dict) else err)
        except Exception:
            detail = str(getattr(resp, "text", ""))[:160]
        return f"HTTP {status}" + (f"：{detail[:160]}" if detail else "")
    try:
        choices = resp.json()["choices"]
        assert isinstance(choices, list)  # noqa: S101 —— 只验证结构存在
    except Exception:
        return "响应不是 OpenAI 兼容格式（检查 base_url 是否为 …/v1 地址）"
    return ""


class Generator:
    def __init__(self, config: dict = None, stats=None, log=None,
                 system_prompt: str = None, client_factory=None, clock=None,
                 output_sanitizer=None):
        cfg = config or {}
        llm = cfg.get("llm", {}) or {}
        safety = cfg.get("safety", {}) or {}
        persona = cfg.get("persona", {}) or {}

        base_url = str(llm.get("base_url", "")).rstrip("/")
        self._url = f"{base_url}/chat/completions"
        self._api_key = str(llm.get("api_key", ""))
        self._model = str(llm.get("model", ""))
        self._temperature = float(llm.get("temperature", 0.8))
        self._max_tokens = int(llm.get("max_tokens", 120))
        self._timeout_s = float(llm.get("timeout_seconds", 8))
        self._fallback = str(safety.get("fallback_reply", "哈哈这个咱们直播间聊哈"))
        self._fallback_enabled = bool(safety.get("fallback_enabled", True))
        self._system_prompt = system_prompt or build_system_prompt(cfg)
        self._max_reply_chars = int(persona.get("max_reply_chars", 40))
        # 输出侧安全过滤（步骤 4.1）：LLM 话术命中敏感词整条替换为 fallback
        self._output_sanitizer = output_sanitizer or (lambda text: text)

        self._stats = stats
        self._log = log or get_logger("generator")
        self._clock = clock or time.time

        def _default_factory():
            return httpx.AsyncClient(timeout=self._timeout_s)

        self._client_factory = client_factory or _default_factory

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    def set_system_prompt(self, prompt: str, max_reply_chars: int = None) -> None:
        """热替换 system 提示词（v3.1 话术存档切换/保存后无需重启）。

        max_reply_chars 一并更新时清洗截断同步生效；None 保持现值。
        """
        prompt = str(prompt or "").strip()
        if prompt:
            self._system_prompt = prompt
        if max_reply_chars:
            self._max_reply_chars = int(max_reply_chars)
        self._log.info("system_prompt_swapped", {"chars": len(self._system_prompt),
                                                 "max_reply_chars": self._max_reply_chars})

    def set_fallback(self, text: str = None, enabled: bool = None) -> None:
        """热替换兜底口播文案/开关（保存后无需重启）；None 保持现值。"""
        if text is not None:
            self._fallback = str(text).strip()
        if enabled is not None:
            self._fallback_enabled = bool(enabled)
        self._log.info("fallback_swapped", {"enabled": self._fallback_enabled,
                                            "chars": len(self._fallback)})

    async def generate_reply(self, context: dict):
        """生成一条口播话术；内部消化全部失败，最终兜底 fallback，不向上抛异常。

        兜底口播关闭（safety.fallback_enabled=false）时：生成失败返回 None
        （调用方按静默处理，不播兜底文案）；敏感词命中且关闭同理返回 None。
        """
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": self.build_user_message(context)},
        ]
        payload = {
            "model": self._model,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "messages": messages,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}

        started = self._clock()
        # 请求字符量（system+user）无论成败都先算——日报成本核算的消耗口径
        prompt_chars = len(self._system_prompt) + len(messages[1]["content"])
        for attempt in (1, 2):  # 原样重试 1 次（PROMPTS 步骤 3.1 降级链）
            try:
                text = self._output_sanitizer(await self._request(payload, headers))
                if not text:
                    # 敏感词命中且兜底口播关闭：静默返回（不重试、不兜底）
                    return None
                elapsed = self._clock() - started
                if self._stats is not None:
                    self._stats.inc("reply_generated")
                self._log.info("llm_reply_generated", {
                    "attempt": attempt, "elapsed_s": round(elapsed, 2),
                    "merged_count": context.get("merged_count", 1),
                    "text": text,
                    "prompt_chars": prompt_chars, "reply_chars": len(text),
                })
                safe_print(f"[LLM] {elapsed:.2f}s 「{text}」")
                return text
            except Exception as e:
                self._fail(f"{type(e).__name__}: {e}", attempt, prompt_chars)
        return self._fallback if self._fallback_enabled else None

    def build_user_message(self, context: dict) -> str:
        """聚合组逐行「观众昵称：内容」（实现位于 hub.prompt）。"""
        return build_user_message(context)

    # ---------- template 方案：应答选择（v3.1） ----------

    async def select_reply(self, danmaku_text: str, candidates: list) -> str | None:
        """从候选参考回复里选一条最贴合弹幕的，返回候选 id；无合适返回 None。

        candidates: [{"id","kind","match","reply"}]——kind=sample/topic，
        match=候选适用场景（样本类型名+弹幕示例 / 话题标题+关键词），reply=应答内容。
        LLM 只输出候选编号（0=都不合适）；编号越界/无法解析按「未选中」处理。
        请求失败（网络/HTTP/解析）重试 1 次后抛 LLMError，由调用方决定降级。
        """
        if not candidates:
            return None
        lines = [f"观众弹幕：{danmaku_text}", "", "候选回复："]
        for i, c in enumerate(candidates, 1):
            lines.append(f"{i}. 适用：{c.get('match', '')}｜回复：{c.get('reply', '')}")
        messages = [
            {"role": "system", "content": SELECT_SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(lines)},
        ]
        payload = {"model": self._model, "temperature": 0,
                   "max_tokens": SELECT_MAX_TOKENS, "messages": messages}
        headers = {"Authorization": f"Bearer {self._api_key}"}

        started = self._clock()
        raw = ""
        for attempt in (1, 2):  # 与 generate_reply 相同的降级链：原样重试 1 次
            try:
                raw = await self._request_raw(payload, headers)
                break
            except Exception as e:
                self._fail(f"{type(e).__name__}: {e}", attempt)
        else:
            raise LLMError("选择请求连续失败")
        text = raw.strip()
        index = _parse_selection(text)
        elapsed = self._clock() - started
        chosen = ""
        if index is not None and 1 <= index <= len(candidates):
            chosen = str(candidates[index - 1].get("id") or "")
        elif index:  # 非 0 越界编号（0 是合法的「都不合适」）
            self._log.warning("llm_select_out_of_range",
                              {"index": index, "candidates": len(candidates)})
        if self._stats is not None:
            self._stats.inc("llm_select")
        self._log.info("llm_select_reply", {
            "elapsed_s": round(elapsed, 2), "raw": text[:20],
            "chosen": chosen or None, "candidates": len(candidates)})
        return chosen or None

    async def _request(self, payload: dict, headers: dict) -> str:
        text = await self._request_raw(payload, headers)
        cleaned = clean_reply(text, self._max_reply_chars)
        if not cleaned:
            raise LLMError("清洗后话术为空（纯 emoji/引号/空白）")
        return cleaned

    async def _request_raw(self, payload: dict, headers: dict) -> str:
        """chat completions 原始文本（不做话术清洗——选择器等非口播调用用）。"""
        async with self._client_factory() as client:
            resp = await client.post(self._url, json=payload, headers=headers)
        # 留痕（debug）：完整请求与响应原文（DESIGN §9 调教数据原料）
        self._log.debug("llm_request", {"url": self._url, "payload": payload})
        self._log.debug("llm_response_raw", {
            "status_code": getattr(resp, "status_code", None),
            "body": _safe_body(resp),
        })
        status = getattr(resp, "status_code", 0)
        if status != 200:
            raise LLMError(f"HTTP {status}")
        try:
            body = resp.json()
            text = body["choices"][0]["message"]["content"]
        except (TypeError, KeyError, IndexError, ValueError) as e:
            raise LLMError(f"choices 解析失败: {e}") from e
        if not str(text or "").strip():
            raise LLMError("响应文本为空")
        return str(text)

    def _fail(self, reason: str, attempt: int, prompt_chars: int = 0) -> None:
        if self._stats is not None:
            self._stats.inc("llm_fail")
        self._log.warning("llm_request_failed", {
            "attempt": attempt, "reason": reason, "prompt_chars": prompt_chars})


def _safe_body(resp) -> str:
    try:
        return str(resp.json())
    except Exception:
        return str(getattr(resp, "text", ""))[:500]
