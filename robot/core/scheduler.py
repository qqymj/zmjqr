"""主动服务调度（M6，技术方案 §5.7）。

统一管理"机器人主动开口"的行为，并做打扰控制，避免它变成话痨：

- **触发**：``at`` 时间窗口（``"08:00-09:30"``）或单点（``"23:30"``）、
  ``every_minutes`` 间隔、``sensor`` 传感器变化（订阅 ``sensor.changed``，§9.1）；
- **条件**：安静时段、每日主动次数上限、距上次互动的最小间隔、仅 IDLE 时开口；
- **动作**：广播 ``schedule.fired``（§9.1 约定订阅方是 fsm），由调用方决定怎么说。

时间（``now_fn``）与"是否空闲"（``can_speak``）都可注入，方便单测；
没有传感器时用 ``push_sensor`` 模拟 HAL 上报，接上 AHT20/BH1750 后换成真实发布即可。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any

from .eventbus import EventBus

DEFAULT_TEXT = "在忙吗？我来陪你说说话。"


def _parse_clock(value: str) -> time:
    hour, minute = value.strip().split(":")
    return time(int(hour), int(minute))


def parse_window(at: str) -> tuple[time, time | None]:
    """``"08:00-09:30"`` -> (08:00, 09:30)；``"23:30"`` -> (23:30, None)。

    单点表示"到点之后触发一次"（跨过该时刻后仍会补触发，避免错过）。
    """
    if "-" in at:
        start, end = at.split("-", 1)
        return _parse_clock(start), _parse_clock(end)
    return _parse_clock(at), None


@dataclass
class Task:
    """一条主动任务。``text`` 缺省时用角色卡问候语。

    ``source`` 指向注册在调度器上的文本源（如 weather/briefing，见 services/）：
    源返回空则本次不开口，避免硬凑内容。
    """

    id: str
    text: str | None = None
    at: str | None = None
    every_minutes: int | None = None
    sensor: str | None = None
    delta: float | None = None
    min_gap_minutes: int = 0
    enabled: bool = True
    source: str | None = None


class Scheduler:
    def __init__(
        self,
        bus: EventBus,
        cfg: dict[str, Any],
        *,
        now_fn: Callable[[], datetime] | None = None,
        can_speak: Callable[[], bool] | None = None,
        greet_text: Callable[[], str] | None = None,
    ) -> None:
        self.bus = bus
        self.enabled = bool(cfg.get("enabled", True))
        self.now_fn = now_fn or datetime.now
        self.can_speak = can_speak or (lambda: True)
        self.greet_text = greet_text
        self.quiet_start = self.quiet_end = None
        quiet = cfg.get("quiet_hours")
        if quiet:
            start, end = parse_window(quiet)
            if end is not None:  # 安静时段必须是区间，单点（"23:00"）无意义
                self.quiet_start, self.quiet_end = start, end
        self.max_daily = int(cfg.get("max_daily", 5))
        self.default_gap_minutes = int(cfg.get("default_gap_minutes", 0))
        self.tick_interval_s = float(cfg.get("tick_interval_s", 20.0))
        self.tasks = [
            task for raw in cfg.get("tasks", []) if (task := self._build(raw)) is not None
        ]
        self._last_interaction: datetime | None = None
        self.sources: dict[str, Callable[[], Awaitable[str | None]]] = {}
        self._fired_on: dict[str, str] = {}  # task.id -> 日期（当天已触发）
        self._every_last: dict[str, datetime] = {}
        self._sensor_values: dict[str, float] = {}
        self._count_date: str | None = None
        self._daily_count = 0
        self._loop_task: asyncio.Task | None = None
        bus.on("sensor.changed", self._on_sensor)
        bus.on("utterance.completed", self._on_interaction)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """启动轮询协程（无任务或未启用时不启动）。"""
        if self._loop_task is None and self.enabled and self.tasks:
            self._loop_task = asyncio.create_task(self._loop())

    async def aclose(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.tick_interval_s)
            try:
                await self.check_once()
            except Exception as exc:  # 调度异常不能拖垮主链路
                await self.bus.emit(
                    "scheduler.failed", {"error": f"{type(exc).__name__}: {exc}"}
                )

    # ------------------------------------------------------------------ #
    # 调度
    # ------------------------------------------------------------------ #

    async def check_once(self, now: datetime | None = None) -> list[str]:
        """按当前时间评估所有定时任务，返回本轮触发的任务 id。"""
        if not self.enabled:
            return []
        now = now or self.now_fn()
        self._roll_date(now)
        fired: list[str] = []
        for task in self.tasks:
            if not task.enabled or task.sensor is not None:
                continue  # 传感器任务只由 sensor.changed 驱动
            if not self._time_due(task, now) or not self._allowed(task, now):
                continue
            if (fired_id := await self._fire(task, now)) is not None:
                fired.append(fired_id)
        return fired

    async def fire_now(self, task_id: str) -> str | None:
        """手动触发一次（/sched fire），跳过时间与打扰条件，便于演示与联调。

        任务不存在、或文本源取不到内容时都返回 None。
        """
        task = next((t for t in self.tasks if t.id == task_id), None)
        if task is None:
            return None
        now = self.now_fn()
        self._roll_date(now)
        return await self._fire(task, now)

    def register_source(self, name: str, provider: Callable[[], Awaitable[str | None]]) -> None:
        """注册文本源（如 weather / briefing），供任务的 ``source`` 字段引用。"""
        self.sources[name] = provider

    async def push_sensor(self, source: str, value: float) -> None:
        """模拟 HAL 上报传感器读数（接真硬件后改由驱动发布 sensor.changed）。"""
        await self._on_sensor({"source": source, "value": value})

    def report(self, now: datetime | None = None) -> str:
        """渲染调度器状态，供 /sched 查看。"""
        now = now or self.now_fn()
        self._roll_date(now)
        quiet = "未设置"
        if self.quiet_start is not None and self.quiet_end is not None:
            quiet = (
                f"{self.quiet_start.strftime('%H:%M')}-{self.quiet_end.strftime('%H:%M')}"
            )
        lines = ["===== 主动服务 ====="]
        lines.append(
            f"  状态：{'启用' if self.enabled else '停用'}　"
            f"今日已主动 {self._daily_count}/{self.max_daily} 次　安静时段：{quiet}"
        )
        lines.append(f"  最近互动：{self._last_interaction or '（本会话暂无）'}")
        if not self.tasks:
            lines.append("  （没有配置任务）")
        for task in self.tasks:
            trigger = task.at or f"每 {task.every_minutes} 分钟"
            if task.sensor:
                trigger = f"传感器 {task.sensor}（变化 ≥ {task.delta}）"
            done = "已触发" if self._fired_on.get(task.id) == now.date().isoformat() else "待触发"
            if task.text:
                content = task.text
            elif task.source:
                content = f"（取自源 {task.source}）"
            else:
                content = "（用角色卡问候）"
            lines.append(f"  - {task.id}　{trigger}　[{done}]　{content}")
        lines.append("  例：/sched fire morning_greet　/sched sense humidity 30")
        lines.append("====================")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 触发判定
    # ------------------------------------------------------------------ #

    def _build(self, raw: dict[str, Any]) -> Task | None:
        if not raw.get("id"):
            return None
        return Task(
            id=str(raw["id"]),
            text=raw.get("text"),
            at=raw.get("at"),
            every_minutes=raw.get("every_minutes"),
            sensor=raw.get("sensor"),
            delta=raw.get("delta"),
            min_gap_minutes=int(raw.get("min_gap_minutes", self.default_gap_minutes)),
            enabled=bool(raw.get("enabled", True)),
            source=raw.get("source"),
        )

    def _roll_date(self, now: datetime) -> None:
        today = now.date().isoformat()
        if today != self._count_date:
            self._count_date = today
            self._daily_count = 0

    def _in_quiet(self, moment: time) -> bool:
        if self.quiet_start is None or self.quiet_end is None:
            return False
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= moment < self.quiet_end
        return moment >= self.quiet_start or moment < self.quiet_end  # 跨午夜

    def _in_window(self, at: str, now: datetime) -> bool:
        start, end = parse_window(at)
        moment = now.time()
        if end is None:
            return moment >= start
        if start <= end:
            return start <= moment <= end
        return moment >= start or moment <= end  # 跨午夜窗口

    def _time_due(self, task: Task, now: datetime) -> bool:
        if task.every_minutes:
            if task.at and not self._in_window(task.at, now):
                return False
            last = self._every_last.get(task.id)
            if last is None:
                self._every_last[task.id] = now  # 首次登记，避免一启动就触发
                return False
            return (now - last).total_seconds() >= task.every_minutes * 60
        if task.at:
            if not self._in_window(task.at, now):
                return False
            return self._fired_on.get(task.id) != now.date().isoformat()
        return False

    def _allowed(self, task: Task, now: datetime) -> bool:
        """打扰控制：安静时段、每日上限、最短互动间隔、是否空闲。"""
        if self._in_quiet(now.time()):
            return False
        if self._daily_count >= self.max_daily:
            return False
        if not self.can_speak():
            return False
        if task.min_gap_minutes and self._last_interaction is not None:
            gap_min = (now - self._last_interaction).total_seconds() / 60
            if gap_min < task.min_gap_minutes:
                return False
        return True

    async def _fire(self, task: Task, now: datetime) -> str | None:
        text = task.text
        if task.source:
            provider = self.sources.get(task.source)
            provided = await provider() if provider else None
            if not provided:
                return None  # 内容取不到（断网/无日程）就不开口，也不计入每日次数
            text = f"{text}。{provided}" if text else provided
        if not text:
            text = self.greet_text() if self.greet_text else DEFAULT_TEXT
        self._fired_on[task.id] = now.date().isoformat()
        self._every_last[task.id] = now
        self._daily_count += 1
        await self.bus.emit("schedule.fired", {"id": task.id, "text": text})
        return task.id

    # ------------------------------------------------------------------ #
    # 事件入口
    # ------------------------------------------------------------------ #

    async def _on_interaction(self, _payload: Any) -> None:
        self._last_interaction = self.now_fn()

    async def _on_sensor(self, payload: dict[str, Any]) -> None:
        source = payload.get("source")
        if source is None:
            return
        value = float(payload["value"])
        previous = self._sensor_values.get(source)
        self._sensor_values[source] = value
        if previous is None:
            return  # 首个读数只作为基线，不触发
        now = self.now_fn()
        self._roll_date(now)
        for task in self.tasks:
            if not task.enabled or task.sensor != source:
                continue
            if task.delta is not None and abs(value - previous) < task.delta:
                continue
            if task.at and not self._in_window(task.at, now):
                continue
            if not self._allowed(task, now):
                continue
            await self._fire(task, now)
