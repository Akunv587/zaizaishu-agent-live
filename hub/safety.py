"""内容安全两级过滤（PROMPTS 步骤 4.1，DESIGN §4.7；PRD FR-6）。

输入侧 check_input 为纯谓词（Router 拿到 True 自行丢弃并计 blocked_input，
避免双重计数）；输出侧 sanitize_output 命中即整条替换为 fallback 并计
blocked_output。匹配为大小写不敏感的包含判断，不做分词与变体识别
（谐音绕过由提示词行为约束兜底，PRD §7）。
"""
from pathlib import Path

from hub.logger import get_logger

DEFAULT_FALLBACK = "哈哈这个咱们直播间聊哈"


class SafetyError(Exception):
    """词库加载失败（文件不存在或无有效词条）。"""


def load_words(path) -> list:
    """一行一词；# 开头与空白行忽略。文件缺失抛 SafetyError。"""
    p = Path(path)
    if not p.exists():
        raise SafetyError(f"敏感词库文件不存在: {p}")
    words = []
    try:
        # utf-8-sig：兼容编辑器加的 BOM（否则首行 # 注释会被读成词条）
        with open(p, "r", encoding="utf-8-sig") as f:
            for line in f:
                word = line.strip()
                if not word or word.startswith("#"):
                    continue
                words.append(word)
    except OSError as e:
        raise SafetyError(f"敏感词库读取失败: {e}") from e
    return words


class SafetyFilter:
    def __init__(self, words, fallback_reply: str = None, stats=None, log=None,
                 fallback_enabled: bool = True):
        self._words = sorted({str(w).lower() for w in words if str(w).strip()})
        self._fallback = str(fallback_reply or DEFAULT_FALLBACK)
        self._fallback_enabled = bool(fallback_enabled)
        self._stats = stats
        self._log = log or get_logger("safety")

    @classmethod
    def from_config(cls, cfg: dict, stats=None, log=None):
        safety = (cfg or {}).get("safety") or {}
        path = str(safety.get("block_words_file", "") or "blockwords.txt").strip() or "blockwords.txt"
        words = load_words(path)  # 文件缺失/读取失败抛 SafetyError
        if not words:
            raise SafetyError(f"词库无有效词条（一行一词，# 开头为注释）: {path}")
        return cls(words, fallback_reply=safety.get("fallback_reply"),
                   fallback_enabled=safety.get("fallback_enabled", True),
                   stats=stats, log=log)

    def set_fallback(self, text: str = None, enabled: bool = None) -> None:
        """热替换兜底文案/开关（保存后无需重启）；None 保持现值。"""
        if text is not None:
            self._fallback = str(text).strip()
        if enabled is not None:
            self._fallback_enabled = bool(enabled)

    @property
    def word_count(self) -> int:
        """自检展示用（PROMPTS 步骤 5.3 启动自检打印词条数）。"""
        return len(self._words)

    def _hit(self, text) -> str:
        t = str(text or "").lower()
        for w in self._words:
            if w in t:
                return w
        return ""

    def check_input(self, text) -> bool:
        """输入侧谓词：命中返回 True（计数与日志由调用方 Router 负责）。"""
        return self._hit(text) != ""

    def sanitize_output(self, text, fallback: str = None) -> str:
        """输出侧：命中整条替换为 fallback 并计 blocked_output；
        兜底口播关闭时返回空串（调用方按静默处理）。"""
        hit = self._hit(text)
        if not hit:
            return str(text)
        if self._stats is not None:
            self._stats.inc("blocked_output")
        self._log.warning("safety_output_replaced", {"word": hit, "text": str(text)[:50]})
        if not self._fallback_enabled:
            return ""
        return str(fallback or self._fallback)
