"""M6 主动服务调度器单测：触发窗口、打扰控制、传感器触发。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from robot.core.eventbus import EventBus
from robot.core.scheduler import DEFAULT_TEXT, Scheduler, parse_window


class Clock:
    """可推进的假时钟，避免测试依赖真实时间。"""

    def __init__(self, moment: datetime) -> None:
        self.now = moment

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def _make(cfg: dict, clock: Clock, **kwargs) -> tuple[Scheduler, list[dict]]:
    bus = EventBus()
    fired: list[dict] = []

    async def on_fired(payload: dict) -> None:
        fired.append(payload)

    bus.on("schedule.fired", on_fired)
    scheduler = Scheduler(bus, cfg, now_fn=clock, **kwargs)
    return scheduler, fired


class TestParseWindow(unittest.TestCase):
    def test_window_and_point(self) -> None:
        start, end = parse_window("08:00-09:30")
        self.assertEqual((start.hour, start.minute, end.hour, end.minute), (8, 0, 9, 30))
        start, end = parse_window("23:00")
        self.assertEqual(start.hour, 23)
        self.assertIsNone(end)  # 单点 = 到点后触发一次


class TestTimeTriggers(unittest.IsolatedAsyncioTestCase):
    async def test_window_fires_once_per_day(self) -> None:
        clock = Clock(datetime(2026, 10, 20, 9, 0))
        scheduler, fired = _make(
            {"tasks": [{"id": "morning", "at": "08:00-09:30"}]}, clock
        )
        self.assertEqual(await scheduler.check_once(), ["morning"])
        self.assertEqual(await scheduler.check_once(), [])  # 当天不重复
        clock.advance(days=1)
        self.assertEqual(await scheduler.check_once(), ["morning"])  # 次日再来一次
        self.assertEqual(len(fired), 2)

    async def test_window_outside_does_not_fire(self) -> None:
        clock = Clock(datetime(2026, 10, 20, 7, 30))
        scheduler, _fired = _make({"tasks": [{"id": "morning", "at": "08:00-09:30"}]}, clock)
        self.assertEqual(await scheduler.check_once(), [])

    async def test_point_fires_after_moment(self) -> None:
        clock = Clock(datetime(2026, 10, 20, 22, 0))
        scheduler, _fired = _make({"tasks": [{"id": "night", "at": "23:00"}]}, clock)
        self.assertEqual(await scheduler.check_once(), [])
        clock.advance(hours=1, minutes=5)
        self.assertEqual(await scheduler.check_once(), ["night"])

    async def test_every_minutes_needs_interval(self) -> None:
        clock = Clock(datetime(2026, 10, 20, 10, 0))
        scheduler, _fired = _make(
            {"tasks": [{"id": "water", "every_minutes": 90}]}, clock
        )
        self.assertEqual(await scheduler.check_once(), [])  # 首次只登记基线
        clock.advance(minutes=89)
        self.assertEqual(await scheduler.check_once(), [])
        clock.advance(minutes=1)
        self.assertEqual(await scheduler.check_once(), ["water"])


class TestDisturbControl(unittest.IsolatedAsyncioTestCase):
    async def test_quiet_hours_block(self) -> None:
        clock = Clock(datetime(2026, 10, 20, 23, 30))
        scheduler, _fired = _make(
            {"quiet_hours": "23:00-07:00", "tasks": [{"id": "night", "at": "23:00"}]},
            clock,
        )
        self.assertEqual(await scheduler.check_once(), [])

    async def test_quiet_hours_cross_midnight(self) -> None:
        clock = Clock(datetime(2026, 10, 20, 6, 0))
        scheduler, _fired = _make(
            {"quiet_hours": "23:00-07:00", "tasks": [{"id": "morning", "at": "05:00"}]},
            clock,
        )
        self.assertEqual(await scheduler.check_once(), [])

    async def test_daily_cap(self) -> None:
        clock = Clock(datetime(2026, 10, 20, 10, 0))
        cfg = {
            "max_daily": 1,
            "tasks": [
                {"id": "a", "at": "09:00"},
                {"id": "b", "at": "09:00"},
            ],
        }
        scheduler, _fired = _make(cfg, clock)
        self.assertEqual(await scheduler.check_once(), ["a"])  # b 被每日上限挡住
        self.assertEqual(scheduler._daily_count, 1)

    async def test_can_speak_gate(self) -> None:
        clock = Clock(datetime(2026, 10, 20, 10, 0))
        scheduler, _fired = _make(
            {"tasks": [{"id": "a", "at": "09:00"}]}, clock, can_speak=lambda: False
        )
        self.assertEqual(await scheduler.check_once(), [])

    async def test_min_gap_since_interaction(self) -> None:
        clock = Clock(datetime(2026, 10, 20, 10, 0))
        scheduler, _fired = _make(
            {"default_gap_minutes": 120, "tasks": [{"id": "a", "at": "09:00"}]}, clock
        )
        await scheduler._on_interaction(None)  # 刚聊完
        self.assertEqual(await scheduler.check_once(), [])
        clock.advance(hours=3)
        self.assertEqual(await scheduler.check_once(), ["a"])

    async def test_disabled_scheduler(self) -> None:
        clock = Clock(datetime(2026, 10, 20, 10, 0))
        scheduler, _fired = _make(
            {"enabled": False, "tasks": [{"id": "a", "at": "09:00"}]}, clock
        )
        self.assertEqual(await scheduler.check_once(), [])


class TestSensorTrigger(unittest.IsolatedAsyncioTestCase):
    async def test_delta_threshold(self) -> None:
        clock = Clock(datetime(2026, 10, 20, 10, 0))
        scheduler, fired = _make(
            {"tasks": [{"id": "dry", "sensor": "humidity", "delta": 8, "text": "干"}]},
            clock,
        )
        await scheduler.push_sensor("humidity", 60)
        self.assertEqual(fired, [])  # 首个读数只作基线
        await scheduler.push_sensor("humidity", 55)
        self.assertEqual(fired, [])  # 变化 5 < 8
        await scheduler.push_sensor("humidity", 40)
        self.assertEqual(fired[-1]["text"], "干")  # 变化 15 ≥ 8

    async def test_sensor_respects_quiet_hours(self) -> None:
        clock = Clock(datetime(2026, 10, 20, 23, 30))
        scheduler, fired = _make(
            {
                "quiet_hours": "23:00-07:00",
                "tasks": [{"id": "dry", "sensor": "humidity", "delta": 5}],
            },
            clock,
        )
        await scheduler.push_sensor("humidity", 60)
        await scheduler.push_sensor("humidity", 40)
        self.assertEqual(fired, [])


class TestTextAndReport(unittest.IsolatedAsyncioTestCase):
    async def test_text_fallback_and_greet_text(self) -> None:
        clock = Clock(datetime(2026, 10, 20, 10, 0))
        scheduler, fired = _make({"tasks": [{"id": "a", "at": "09:00"}]}, clock)
        await scheduler.check_once()
        self.assertEqual(fired[-1]["text"], DEFAULT_TEXT)

        clock2 = Clock(datetime(2026, 10, 20, 10, 0))
        scheduler2, fired2 = _make(
            {"tasks": [{"id": "b", "at": "09:00"}]}, clock2, greet_text=lambda: "早呀"
        )
        await scheduler2.check_once()
        self.assertEqual(fired2[-1]["text"], "早呀")

    async def test_fire_now_bypasses_conditions(self) -> None:
        clock = Clock(datetime(2026, 10, 20, 23, 30))
        scheduler, fired = _make(
            {"quiet_hours": "23:00-07:00", "tasks": [{"id": "a", "at": "09:00"}]}, clock
        )
        self.assertEqual(await scheduler.fire_now("a"), "a")  # 安静时段也能手动触发
        self.assertIsNone(await scheduler.fire_now("missing"))
        self.assertEqual(len(fired), 1)

    async def test_report_lists_tasks(self) -> None:
        clock = Clock(datetime(2026, 10, 20, 10, 0))
        scheduler, _fired = _make(
            {"tasks": [{"id": "morning_greet", "at": "08:00-09:30"}]}, clock
        )
        report = scheduler.report()
        self.assertIn("morning_greet", report)
        self.assertIn("今日已主动 0/5", report)


if __name__ == "__main__":
    unittest.main()
