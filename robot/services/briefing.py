"""早间播报（M6）：把天气与日程拼成一句话。

某个来源取不到内容（断网、无订阅、今天没安排）就整段省略，
两个来源都空则返回 ``None`` —— 由调度器决定干脆不开口（不硬凑话）。
"""

from __future__ import annotations

from .calendar import CalendarService
from .weather import WeatherService


class BriefingService:
    def __init__(self, weather: WeatherService, calendar: CalendarService) -> None:
        self.weather = weather
        self.calendar = calendar

    async def report(self) -> str | None:
        parts: list[str] = []
        for provider in (self.weather.report_text, self.calendar.report_text):
            text = await provider()
            if text:
                parts.append(text.rstrip("。"))
        return "。".join(parts) + "。" if parts else None
