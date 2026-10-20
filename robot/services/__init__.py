"""内容服务（M6，技术方案 §5.1 services/）。

- ``weather``：Open-Meteo 天气（免 API Key）；
- ``calendar``：iCalendar 日程（本地 .ics 或 http(s) 订阅）；
- ``news``：RSS / Atom 订阅聚合（免 API Key）；
- ``briefing``：把上面几者拼成早间播报。

全部只读拉取，失败即返回 ``None``：主动服务取不到内容就保持安静。
"""

from .briefing import BriefingService
from .calendar import CalendarService, Event, parse_ics
from .news import NewsService, parse_feed
from .weather import WeatherService, describe_code

__all__ = [
    "BriefingService",
    "CalendarService",
    "Event",
    "NewsService",
    "WeatherService",
    "describe_code",
    "parse_feed",
    "parse_ics",
]
