"""LLM 路由：主通道失败或首 token 超时，自动降级到备用通道。

对应技术方案 §3.2（端云协同）与 §7.5（稳定性与降级）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from ..core.eventbus import EventBus


class LLMRouter:
    def __init__(
        self,
        bus: EventBus,
        primary,
        fallback=None,
        first_token_timeout: float = 3.0,
    ) -> None:
        self.bus = bus
        self.primary = primary
        self.fallback = fallback
        self.first_token_timeout = first_token_timeout

    @property
    def name(self) -> str:
        return self.primary.name

    async def stream(self, messages) -> AsyncIterator[str]:
        messages = list(messages)
        try:
            agen = self.primary.stream(messages)
            # 只对"首 token"设超时；后续 token 走客户端整体超时
            first = await asyncio.wait_for(
                agen.__anext__(), self.first_token_timeout
            )
        except Exception as exc:  # 含超时、网络错误、空回复(StopAsyncIteration)
            if self.fallback is None:
                raise
            await self.bus.emit(
                "llm.fallback",
                {
                    "from": self.primary.name,
                    "to": self.fallback.name,
                    "reason": type(exc).__name__,
                },
            )
            agen = self.fallback.stream(messages)
            first = await agen.__anext__()

        yield first
        async for token in agen:
            yield token
