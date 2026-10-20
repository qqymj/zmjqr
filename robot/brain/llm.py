"""LLM 客户端：本地 Ollama / 云端 OpenAI 兼容 / 离线桩。

三种实现共用同一接口，上层（流水线、路由）无需感知具体实现：

    async def stream(self, messages) -> AsyncIterator[str]
    name: str   # 供日志与降级事件使用
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable

try:
    import httpx
except ImportError:  # 未安装 httpx 时仍可使用离线桩
    httpx = None  # type: ignore[assignment]


def _ensure_httpx() -> None:
    if httpx is None:
        raise RuntimeError("未安装 httpx，无法使用网络 LLM；请先执行 uv sync")


# --------------------------------------------------------------------------- #
# 离线桩（M0 沿用，--fake 时使用）
# --------------------------------------------------------------------------- #

# (关键词组, 回复话术)；回复刻意写成 1~2 句，用于验证按句流式切分
REPLIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("累", "难过", "烦", "压力", "emo", "不想", "难受", "委屈"),
        "辛苦啦，先歇一会儿也没关系。今天发生什么了，想跟我讲讲吗？",
    ),
    (
        ("开心", "高兴", "棒", "太好了", "哈哈", "爽"),
        "看你这么开心，我也跟着高兴起来了。快跟我说说是什么好事？",
    ),
    (
        ("焦虑", "紧张", "害怕", "担心", "慌"),
        "别急，慢慢来。先深呼吸一下，我们一起把这件事捋一捋。",
    ),
    (
        ("你好", "在吗", "hi", "hello", "早上好", "晚安"),
        "嗯嗯，我在听~ 今天过得怎么样？",
    ),
)

DEFAULT_REPLY = "嗯，我在听着呢。然后呢，你接着说？"


class EchoLLM:
    """逐字流式输出的桩模型，char_delay=0 时即刻返回（供单测）。"""

    name = "echo"

    def __init__(self, char_delay: float = 0.01) -> None:
        self.char_delay = char_delay

    async def stream(self, messages: Iterable[dict]) -> AsyncIterator[str]:
        messages = list(messages)
        user_text = messages[-1]["content"] if messages else ""
        for ch in self._reply(user_text):
            yield ch
            if self.char_delay:
                await asyncio.sleep(self.char_delay)

    @staticmethod
    def _reply(text: str) -> str:
        for keywords, reply in REPLIES:
            if any(k in text for k in keywords):
                return reply
        return DEFAULT_REPLY


# --------------------------------------------------------------------------- #
# 本地 Ollama：POST /api/chat，NDJSON 流式
# --------------------------------------------------------------------------- #


class OllamaLLM:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        _ensure_httpx()
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    @property
    def name(self) -> str:
        return f"ollama:{self.model}"

    async def stream(self, messages: Iterable[dict]) -> AsyncIterator[str]:
        payload = {"model": self.model, "messages": list(messages), "stream": True}
        url = f"{self.base_url}/api/chat"
        async with self._client.stream("POST", url, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                data = json.loads(line)
                chunk = data.get("message", {}).get("content", "")
                if chunk:
                    yield chunk
                if data.get("done"):
                    break

    async def aclose(self) -> None:
        await self._client.aclose()


# --------------------------------------------------------------------------- #
# 云端 OpenAI 兼容：POST /v1/chat/completions，SSE 流式
# 适用于 OpenAI / DeepSeek / Moonshot / 通义千问兼容模式等
# --------------------------------------------------------------------------- #


class OpenAICompatLLM:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        extra_body: dict | None = None,
    ) -> None:
        _ensure_httpx()
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.extra_body = extra_body or {}
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            timeout=timeout, headers=headers, transport=transport
        )

    @property
    def name(self) -> str:
        return f"cloud:{self.model}"

    async def stream(self, messages: Iterable[dict]) -> AsyncIterator[str]:
        payload = {
            "model": self.model,
            "messages": list(messages),
            "stream": True,
            **self.extra_body,  # 供应商专有参数，如 enable_thinking
        }
        url = f"{self.base_url}/chat/completions"
        async with self._client.stream("POST", url, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:") :].strip()
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                # 只取 content；思考模型的 reasoning_content 直接忽略
                chunk = delta.get("content") or ""
                if chunk:
                    yield chunk

    async def aclose(self) -> None:
        await self._client.aclose()
