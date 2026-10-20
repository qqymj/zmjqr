"""事件总线：模块间以事件解耦，便于单测与替换。

设计要点见技术方案 §9.1。所有处理器均为 async 协程；
单个订阅者抛异常不影响其他订阅者。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

Handler = Callable[[Any], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)

    def on(self, event: str, handler: Handler) -> None:
        """订阅事件。"""
        self._subs[event].append(handler)

    def off(self, event: str, handler: Handler) -> None:
        """取消订阅。"""
        if handler in self._subs.get(event, []):
            self._subs[event].remove(handler)

    async def emit(self, event: str, payload: Any = None) -> None:
        """发布事件，并发通知所有订阅者；异常被隔离。"""
        handlers = list(self._subs.get(event, []))
        if not handlers:
            return
        await asyncio.gather(
            *(h(payload) for h in handlers), return_exceptions=True
        )

    def subscribers(self, event: str) -> int:
        """返回某事件的订阅者数量（用于测试与调试）。"""
        return len(self._subs.get(event, []))
