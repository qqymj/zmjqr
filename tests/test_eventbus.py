"""事件总线单测：验证订阅、并发分发与异常隔离。

用标准库 unittest，无需安装额外依赖：
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from robot.core.eventbus import EventBus


class TestEventBus(unittest.IsolatedAsyncioTestCase):
    async def test_emit_reaches_all_subscribers(self) -> None:
        bus = EventBus()
        seen: list[str] = []

        async def handler_a(payload):
            seen.append(f"a:{payload}")

        async def handler_b(payload):
            seen.append(f"b:{payload}")

        bus.on("ping", handler_a)
        bus.on("ping", handler_b)
        await bus.emit("ping", "hello")

        self.assertEqual(sorted(seen), ["a:hello", "b:hello"])
        self.assertEqual(bus.subscribers("ping"), 2)

    async def test_no_subscriber_is_noop(self) -> None:
        bus = EventBus()
        await bus.emit("nobody.listens")  # 不应抛异常

    async def test_handler_exception_is_isolated(self) -> None:
        bus = EventBus()
        seen: list[str] = []

        async def broken(_payload):
            raise RuntimeError("boom")

        async def healthy(payload):
            seen.append(payload)

        bus.on("evt", broken)
        bus.on("evt", healthy)
        await bus.emit("evt", "ok")  # broken 抛错不应影响 healthy

        self.assertEqual(seen, ["ok"])

    async def test_off_unsubscribes(self) -> None:
        bus = EventBus()
        seen: list[str] = []

        async def handler(payload):
            seen.append(payload)

        bus.on("evt", handler)
        bus.off("evt", handler)
        await bus.emit("evt", "x")

        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
