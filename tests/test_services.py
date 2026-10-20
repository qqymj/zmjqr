"""M6 内容服务单测：天气解析/降级、ICS 解析与 RRULE、播报组合、调度器文本源。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from robot.core.eventbus import EventBus
from robot.core.scheduler import Scheduler
from robot.services import (
    BriefingService,
    CalendarService,
    NewsService,
    WeatherService,
    parse_feed,
    parse_ics,
)
from robot.services.weather import describe_code, format_report

TZ = ZoneInfo("Asia/Shanghai")

WEATHER_PAYLOAD = {
    "current": {"temperature_2m": 22.4, "relative_humidity_2m": 97, "weather_code": 3},
    "daily": {
        "temperature_2m_max": [27.9],
        "temperature_2m_min": [13.2],
        "precipitation_probability_max": [85],
        "weather_code": [53],
    },
}


def _weather(fetch=None, **cfg) -> WeatherService:
    base = {"latitude": 31.2, "longitude": 121.4, "city": "上海"}
    return WeatherService({**base, **cfg}, fetch=fetch)


class TestWeather(unittest.IsolatedAsyncioTestCase):
    def test_wmo_code(self) -> None:
        self.assertEqual(describe_code(0), "晴")
        self.assertEqual(describe_code(3), "阴")
        self.assertEqual(describe_code(95), "雷阵雨")
        self.assertEqual(describe_code(12345), "天气不明")
        self.assertEqual(describe_code(None), "天气不明")

    def test_format_report(self) -> None:
        snap = _weather()._parse(WEATHER_PAYLOAD)
        assert snap is not None
        text = format_report(snap)
        self.assertIn("上海今天阴", text)
        self.assertIn("13~28℃", text)  # 四舍五入
        self.assertIn("降水概率 85%", text)

    async def test_snapshot_and_report(self) -> None:
        async def fake_fetch(url: str, params: dict) -> dict:
            self.assertIn("latitude", params)
            return WEATHER_PAYLOAD

        weather = _weather(fake_fetch)
        snap = await weather.snapshot()
        assert snap is not None
        self.assertEqual(snap["temperature"], 22)
        self.assertEqual(snap["rain_prob"], 85)
        self.assertTrue((await weather.report_text()).startswith("上海今天"))

    async def test_fetch_failure_is_silent(self) -> None:
        async def boom(url: str, params: dict) -> dict:
            raise RuntimeError("网络不可用")

        weather = _weather(boom)
        self.assertIsNone(await weather.snapshot())
        self.assertIsNone(await weather.report_text())  # 取不到就不开口
        self.assertIsNone(await weather.rain_alert_text())

    async def test_no_coordinates(self) -> None:
        weather = WeatherService({"city": "上海"})
        self.assertIsNone(await weather.snapshot())

    async def test_rain_alert_threshold(self) -> None:
        async def fake_fetch(url: str, params: dict) -> dict:
            return WEATHER_PAYLOAD  # 降水概率 85%

        weather = _weather(fake_fetch)
        self.assertIn("带伞", await weather.rain_alert_text(threshold=60))
        self.assertIsNone(await weather.rain_alert_text(threshold=90))  # 未达阈值保持安静

    def test_malformed_payload(self) -> None:
        self.assertIsNone(_weather()._parse({"daily": {}}))


ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:周会
LOCATION:会议室 A
DTSTART:20261020T090000
DTEND:20261020T100000
END:VEVENT
BEGIN:VEVENT
SUMMARY:牙医
DTSTART:20261021T143000
END:VEVENT
BEGIN:VEVENT
SUMMARY:项目评审会
DTSTART:20261019T100000
RRULE:FREQ=WEEKLY;BYDAY=MO,WE
END:VEVENT
BEGIN:VEVENT
SUMMARY:全天团建
DTSTART;VALUE=DATE:20261020
END:VEVENT
END:VCALENDAR
"""

FOLDED = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:需要换行折叠
 的会议标题
DTSTART:20261020T110000
END:VEVENT
END:VCALENDAR
"""

RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>少数派</title>
<item><title>少数派 A 标题</title><link>https://a/1</link></item>
<item><title>少数派 B 标题</title><link>https://a/2</link></item>
</channel></rss>
"""

RSS_OVERLAP = """<rss version="2.0"><channel>
<item><title>少数派 A 标题</title></item>
<item><title>B 站独有标题</title></item>
</channel></rss>
"""

ATOM_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>某博客</title>
<entry><title>Atom 标题</title><id>1</id></entry>
</feed>
"""


class TestIcsParsing(unittest.TestCase):
    def test_parse_fields_and_sort(self) -> None:
        events = parse_ics(ICS, tz=TZ, horizon_end=datetime(2026, 10, 25, tzinfo=TZ))
        summaries = [event.summary for event in events]
        self.assertIn("周会", summaries)
        self.assertEqual([e.start for e in events], sorted(e.start for e in events))
        meeting = next(e for e in events if e.summary == "周会")
        self.assertEqual((meeting.start.hour, meeting.start.minute), (9, 0))
        self.assertEqual(meeting.when, "09:00")
        self.assertEqual(meeting.location, "会议室 A")

    def test_all_day_event(self) -> None:
        events = parse_ics(ICS, tz=TZ)
        all_day = next(e for e in events if e.summary == "全天团建")
        self.assertTrue(all_day.all_day)
        self.assertEqual(all_day.when, "全天")

    def test_folded_line(self) -> None:
        events = parse_ics(FOLDED, tz=TZ)
        self.assertEqual(events[0].summary, "需要换行折叠的会议标题")

    def test_rrule_weekly_expansion(self) -> None:
        events = parse_ics(ICS, tz=TZ, horizon_end=datetime(2026, 11, 2, tzinfo=TZ))
        review = [e for e in events if e.summary == "项目评审会"]
        self.assertGreater(len(review), 2)  # 每周一/三各一次
        self.assertTrue(all(e.start.weekday() in (0, 2) for e in review))

    def test_rrule_until(self) -> None:
        raw = ICS.replace(
            "RRULE:FREQ=WEEKLY;BYDAY=MO,WE", "RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261026T000000Z"
        )
        events = parse_ics(raw, tz=TZ, horizon_end=datetime(2027, 1, 1, tzinfo=TZ))
        occurrences = [e.start for e in events if e.summary == "项目评审会"]
        # UNTIL=2026-10-26 00:00Z（北京 08:00）早于当天 10:00 的那次，故只留首个
        self.assertEqual(occurrences, [datetime(2026, 10, 19, 10, 0, tzinfo=TZ)])

    def test_unknown_freq_kept_single(self) -> None:
        single = parse_ics(
            "BEGIN:VEVENT\nSUMMARY:年度大促\nDTSTART:20261020T090000\n"
            "RRULE:FREQ=MONTHLY\nEND:VEVENT\n",
            tz=TZ,
        )
        self.assertEqual(len(single), 1)  # 暂不支持的频率按单次处理

    def test_broken_lines_are_skipped(self) -> None:
        raw = "BEGIN:VEVENT\nDTSTART:坏数据\nEND:VEVENT\nBEGIN:VEVENT\nEND:VEVENT\n"
        self.assertEqual(parse_ics(raw, tz=TZ), [])
        self.assertEqual(parse_ics("完全不是 ICS", tz=TZ), [])


class TestCalendarService(unittest.IsolatedAsyncioTestCase):
    async def test_local_file_events_today(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cal.ics"
            path.write_text(ICS, encoding="utf-8")
            service = CalendarService(
                {"ics_path": str(path), "timezone": "Asia/Shanghai", "days": 2}
            )
            now = datetime(2026, 10, 20, 8, 0, tzinfo=TZ)
            events = await service.events(now)
            self.assertIn("周会", [e.summary for e in events])
            self.assertIn("全天团建", [e.summary for e in events])  # 当天全天事件要保留
            report = await service.report_text(now)
            self.assertIn("今天有", report)

    async def test_missing_file_is_empty(self) -> None:
        service = CalendarService({"ics_path": "/no/such/file.ics"})
        self.assertEqual(await service.events(datetime(2026, 10, 20, 8, 0, tzinfo=TZ)), [])
        self.assertIsNone(await service.report_text(datetime(2026, 10, 20, 8, 0, tzinfo=TZ)))

    async def test_url_source_and_failure(self) -> None:
        calls: list[str] = []

        async def fetch_text(url: str) -> str:
            calls.append(url)
            return ICS

        service = CalendarService(
            {"ics_url": "https://example.com/cal.ics"}, fetch_text=fetch_text
        )
        now = datetime(2026, 10, 20, 8, 0, tzinfo=TZ)
        self.assertTrue(await service.events(now))
        self.assertEqual(calls, ["https://example.com/cal.ics"])

        async def boom(url: str) -> str:
            raise RuntimeError("订阅拉取失败")

        broken = CalendarService({"ics_url": "https://example.com/x.ics"}, fetch_text=boom)
        self.assertEqual(await broken.events(now), [])

    async def test_empty_schedule_is_silent(self) -> None:
        service = CalendarService({"ics_path": ""})
        self.assertIsNone(await service.report_text())

    async def test_no_events_in_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cal.ics"
            path.write_text(ICS, encoding="utf-8")
            service = CalendarService({"ics_path": str(path), "timezone": "Asia/Shanghai"})
            far = datetime(2026, 12, 25, 8, 0, tzinfo=TZ)
            self.assertEqual(await service.events(far), [])


class TestBriefing(unittest.IsolatedAsyncioTestCase):
    async def test_compose_both(self) -> None:
        async def fake_fetch(url: str, params: dict) -> dict:
            return WEATHER_PAYLOAD

        weather = _weather(fake_fetch)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cal.ics"
            path.write_text(ICS, encoding="utf-8")
            calendar = CalendarService(
                {"ics_path": str(path), "timezone": "Asia/Shanghai"},
                now_fn=lambda: datetime(2026, 10, 20, 8, 0, tzinfo=TZ),
            )
            text = await BriefingService(weather, calendar).report()
            self.assertIn("上海今天阴", text)
            self.assertIn("周会", text)
            self.assertTrue(text.endswith("。"))

    async def test_compose_skips_empty_parts(self) -> None:
        async def boom(url: str, params: dict) -> dict:
            raise RuntimeError("断网")

        weather = _weather(boom)
        calendar = CalendarService({"ics_path": "/no/such.ics"})
        self.assertIsNone(await BriefingService(weather, calendar).report())

    async def test_compose_only_weather(self) -> None:
        async def fake_fetch(url: str, params: dict) -> dict:
            return WEATHER_PAYLOAD

        weather = _weather(fake_fetch)
        calendar = CalendarService({"ics_path": "/no/such.ics"})
        text = await BriefingService(weather, calendar).report()
        self.assertEqual(text, await weather.report_text())  # 无日程时只剩天气那段


class TestSchedulerSource(unittest.IsolatedAsyncioTestCase):
    async def test_source_text_is_spoken(self) -> None:
        bus = EventBus()
        fired: list[dict] = []

        async def on_fired(payload: dict) -> None:
            fired.append(payload)

        bus.on("schedule.fired", on_fired)
        now = datetime(2026, 10, 20, 8, 0)
        scheduler = Scheduler(
            bus,
            {"tasks": [{"id": "brief", "at": "07:30-09:00", "source": "briefing",
                        "text": "早上好呀"}]},
            now_fn=lambda: now,
        )

        async def briefing() -> str:
            return "今天多云。"

        scheduler.register_source("briefing", briefing)
        self.assertEqual(await scheduler.check_once(), ["brief"])
        self.assertEqual(fired[-1]["text"], "早上好呀。今天多云。")  # 前缀 + 源内容

    async def test_empty_source_keeps_silent(self) -> None:
        bus = EventBus()
        fired: list[dict] = []

        async def on_fired(payload: dict) -> None:
            fired.append(payload)

        bus.on("schedule.fired", on_fired)
        now = datetime(2026, 10, 20, 8, 0)
        scheduler = Scheduler(
            bus,
            {"tasks": [{"id": "rain", "at": "07:30-09:00", "source": "rain_alert"}]},
            now_fn=lambda: now,
        )

        async def empty() -> str | None:
            return None

        scheduler.register_source("rain_alert", empty)
        self.assertEqual(await scheduler.check_once(), [])  # 没内容就不开口
        self.assertEqual(fired, [])
        self.assertEqual(scheduler._daily_count, 0)  # 也不计入每日次数
        self.assertIsNone(await scheduler.fire_now("rain"))

    async def test_missing_source_is_silent(self) -> None:
        bus = EventBus()
        now = datetime(2026, 10, 20, 8, 0)
        scheduler = Scheduler(
            bus, {"tasks": [{"id": "x", "at": "07:30-09:00", "source": "unknown"}]},
            now_fn=lambda: now,
        )
        self.assertEqual(await scheduler.check_once(), [])


class TestNews(unittest.IsolatedAsyncioTestCase):
    async def test_rss_and_atom_parsing(self) -> None:
        self.assertEqual(parse_feed(RSS_FEED), ["少数派 A 标题", "少数派 B 标题"])
        self.assertEqual(parse_feed(ATOM_FEED), ["Atom 标题"])

    async def test_broken_or_non_xml(self) -> None:
        self.assertEqual(parse_feed("<!DOCTYPE html><html>not a feed</html>"), [])
        self.assertEqual(parse_feed("<rss><item><title>坏"), [])
        self.assertEqual(parse_feed("<rss><item></item></rss>"), [])  # 缺标题也跳过

    async def test_long_title_truncated(self) -> None:
        raw = f"<rss><item><title>{'长' * 200}</title></item></rss>"
        self.assertEqual(len(parse_feed(raw)[0]), 80)

    async def test_merge_dedupe_and_limit(self) -> None:
        async def fetch_text(url: str) -> str:
            return RSS_FEED if "a.com" in url else RSS_OVERLAP

        news = NewsService(
            {
                "feeds": ["https://a.com/feed", "https://b.com/feed"],
                "per_feed": 2,
                "max_items": 3,
            },
            fetch_text=fetch_text,
        )
        headlines = await news.headlines()
        self.assertEqual(headlines, ["少数派 A 标题", "少数派 B 标题", "B 站独有标题"])
        text = await news.report_text()
        self.assertIn("刚看到几条消息：", text)

    async def test_one_broken_feed_does_not_break_others(self) -> None:
        async def fetch_text(url: str) -> str:
            if "bad" in url:
                raise RuntimeError("拉取失败")
            return RSS_FEED

        news = NewsService(
            {"feeds": ["https://bad.com/feed", "https://good.com/feed"], "per_feed": 1},
            fetch_text=fetch_text,
        )
        self.assertEqual(await news.headlines(), ["少数派 A 标题"])

    async def test_all_sources_down_is_silent(self) -> None:
        async def boom(url: str) -> str:
            raise RuntimeError("断网")

        news = NewsService({"feeds": ["https://a.com/feed"]}, fetch_text=boom)
        self.assertEqual(await news.headlines(), [])
        self.assertIsNone(await news.report_text())

    async def test_empty_feed_list(self) -> None:
        self.assertIsNone(await NewsService({"feeds": []}).report_text())


class TestTimezoneIndependence(unittest.TestCase):
    def test_utc_event_converted_to_config_tz(self) -> None:
        raw = "BEGIN:VEVENT\nSUMMARY:跨时区\nDTSTART:20261020T010000Z\nEND:VEVENT\n"
        events = parse_ics(raw, tz=TZ)
        self.assertEqual(events[0].start.hour, 9)  # UTC 01:00 -> 北京 09:00
        self.assertEqual(events[0].start.utcoffset(), timedelta(hours=8))

    def test_floating_time_uses_config_tz(self) -> None:
        raw = "BEGIN:VEVENT\nSUMMARY:本地时间\nDTSTART:20261020T090000\nEND:VEVENT\n"
        events = parse_ics(raw, tz=TZ)
        self.assertEqual(events[0].start.utcoffset(), timedelta(hours=8))


if __name__ == "__main__":
    unittest.main()
