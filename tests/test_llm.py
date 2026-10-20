"""LLM 客户端与路由单测。

用 httpx.MockTransport 伪造响应，验证流式解析与降级逻辑，无需真实网络。
"""

from __future__ import annotations

import asyncio
import json
import unittest

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

from robot.brain.llm import OllamaLLM, OpenAICompatLLM
from robot.brain.router import LLMRouter
from robot.core.eventbus import EventBus


def _sse_body(chunks: list[str]) -> bytes:
    """构造 OpenAI 兼容的 SSE 响应体。"""
    lines: list[str] = []
    for c in chunks:
        payload = {"choices": [{"delta": {"content": c}}]}
        lines.append("data: " + json.dumps(payload, ensure_ascii=False))
        lines.append("")
    lines.append("data: [DONE]")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _ndjson_body(chunks: list[str]) -> bytes:
    """构造 Ollama 的 NDJSON 响应体。"""
    lines = [
        json.dumps({"message": {"content": c}, "done": False}, ensure_ascii=False)
        for c in chunks
    ]
    lines.append(json.dumps({"message": {"content": ""}, "done": True}))
    return ("\n".join(lines) + "\n").encode("utf-8")


async def _collect(stream) -> str:
    out: list[str] = []
    async for token in stream:
        out.append(token)
    return "".join(out)


# --------------------------------------------------------------------------- #
# 流式解析
# --------------------------------------------------------------------------- #


@unittest.skipIf(httpx is None, "httpx 未安装")
class TestStreamParsing(unittest.IsolatedAsyncioTestCase):
    async def test_openai_compat_sse(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/chat/completions")
            self.assertEqual(request.headers.get("authorization"), "Bearer key")
            return httpx.Response(200, content=_sse_body(["你", "好", "呀"]))

        llm = OpenAICompatLLM(
            "https://example.com/v1", "m", "key", transport=httpx.MockTransport(handler)
        )
        try:
            text = await _collect(llm.stream([{"role": "user", "content": "hi"}]))
        finally:
            await llm.aclose()
        self.assertEqual(text, "你好呀")

    async def test_ollama_ndjson(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/chat")
            return httpx.Response(200, content=_ndjson_body(["嗯", "在听"]))

        llm = OllamaLLM(
            "http://localhost:11434", "qwen", transport=httpx.MockTransport(handler)
        )
        try:
            text = await _collect(llm.stream([{"role": "user", "content": "hi"}]))
        finally:
            await llm.aclose()
        self.assertEqual(text, "嗯在听")

    async def test_extra_body_merged_into_request(self) -> None:
        sent: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            sent.update(json.loads(request.content))
            return httpx.Response(200, content=_sse_body(["ok"]))

        llm = OpenAICompatLLM(
            "https://example.com/v1",
            "m",
            "",
            transport=httpx.MockTransport(handler),
            extra_body={"enable_thinking": False},
        )
        try:
            await _collect(llm.stream([{"role": "user", "content": "hi"}]))
        finally:
            await llm.aclose()

        self.assertIs(sent["enable_thinking"], False)
        self.assertEqual(sent["model"], "m")
        self.assertTrue(sent["stream"])

    async def test_http_error_propagates(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"server error")

        llm = OpenAICompatLLM(
            "https://example.com/v1", "m", "", transport=httpx.MockTransport(handler)
        )
        try:
            with self.assertRaises(httpx.HTTPStatusError):
                await _collect(llm.stream([{"role": "user", "content": "hi"}]))
        finally:
            await llm.aclose()


# --------------------------------------------------------------------------- #
# 路由与降级
# --------------------------------------------------------------------------- #


class _BoomLLM:
    name = "boom"

    async def stream(self, messages):
        raise RuntimeError("network down")
        yield ""  # 使其成为 async generator


class _ListLLM:
    def __init__(self, name: str, chunks: list[str]) -> None:
        self.name = name
        self.chunks = chunks

    async def stream(self, messages):
        for c in self.chunks:
            yield c


class _SlowLLM:
    name = "slow"

    async def stream(self, messages):
        await asyncio.sleep(5)
        yield "never"


class TestLLMRouter(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bus = EventBus()
        self.events: list[dict] = []

        async def on_fallback(payload: dict) -> None:
            self.events.append(payload)

        self.bus.on("llm.fallback", on_fallback)

    async def test_primary_success_no_fallback(self) -> None:
        router = LLMRouter(self.bus, _ListLLM("p", ["甲", "乙"]))
        self.assertEqual(await _collect(router.stream([])), "甲乙")
        self.assertEqual(self.events, [])

    async def test_fallback_on_error(self) -> None:
        router = LLMRouter(self.bus, _BoomLLM(), _ListLLM("fb", ["兜", "底"]))
        self.assertEqual(await _collect(router.stream([])), "兜底")
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["from"], "boom")
        self.assertEqual(self.events[0]["to"], "fb")
        self.assertEqual(self.events[0]["reason"], "RuntimeError")

    async def test_fallback_on_first_token_timeout(self) -> None:
        router = LLMRouter(self.bus, _SlowLLM(), _ListLLM("fb", ["快"]), first_token_timeout=0.05)
        self.assertEqual(await _collect(router.stream([])), "快")
        self.assertEqual(self.events[0]["reason"], "TimeoutError")

    async def test_raises_when_no_fallback(self) -> None:
        router = LLMRouter(self.bus, _BoomLLM())
        with self.assertRaises(RuntimeError):
            await _collect(router.stream([]))


if __name__ == "__main__":
    unittest.main()
