"""日程服务（M6，技术方案 §5.1 services/calendar.py）。

读取 iCalendar（.ics）：本地文件（ics_path）或 http(s) 订阅地址（ics_url，
如手机日历导出的订阅链接），两者都配则合并。零依赖手写最小解析器，只覆盖
VEVENT 的 DTSTART/DTEND/SUMMARY/LOCATION 与常见 RRULE（DAILY / WEEKLY，
含 INTERVAL / BYDAY / COUNT / UNTIL），够桌面陪伴用；MONTHLY / YEARLY 等
按单次事件处理。

时区：所有时间统一挂载配置时区（默认 Asia/Shanghai），因此"今天/明天"的
判断与机器所在时区无关，换环境跑结果一致。

取不到内容和没有安排都返回 ``None``，让调度器保持安静（不硬凑话）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

MAX_ITEMS = 4  # 单日最多播报几个安排，避免一口气念太长
FetchText = Callable[[str], Awaitable[str]]


@dataclass
class Event:
    start: datetime
    end: datetime | None
    summary: str
    all_day: bool = False
    location: str | None = None

    @property
    def when(self) -> str:
        return "全天" if self.all_day else self.start.strftime("%H:%M")


def unfold(raw: str) -> list[str]:
    """还原 ICS 折行：续行以空格或制表符开头，需并回上一行。"""
    lines: list[str] = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def parse_ics(
    raw: str, *, tz: tzinfo, horizon_end: datetime | None = None
) -> list[Event]:
    """解析 ICS 文本，返回按开始时间排序的事件列表。"""
    events: list[Event] = []
    block: dict[str, tuple[str, dict[str, str]]] = {}
    in_event = False
    for line in unfold(raw):
        if line == "BEGIN:VEVENT":
            block, in_event = {}, True
            continue
        if line == "END:VEVENT":
            in_event = False
            events.extend(_build_events(block, tz, horizon_end))
            continue
        if not in_event or ":" not in line:
            continue
        name, _, value = line.partition(":")
        key, _, params = name.partition(";")
        block[key.upper()] = (value, _parse_params(params))
    events.sort(key=lambda e: e.start)
    return events


def _parse_params(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw.split(";"):
        key, sep, value = item.partition("=")
        if sep:
            out[key.upper()] = value.strip('"')
    return out


def _parse_dt(value: str, params: dict[str, str], tz: tzinfo) -> tuple[datetime, bool]:
    """解析 ICS 时间；返回 (时间, 是否全天)。支持 DATE、本地时间、UTC(Z)、TZID。"""
    value = value.strip()
    if value.endswith("Z"):
        moment = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        return moment.astimezone(tz), False
    if len(value) == 8:  # 形如 20261020：全天事件
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=tz), True
    moment = datetime.strptime(value, "%Y%m%dT%H%M%S")
    return moment.replace(tzinfo=_zone(params.get("TZID"), tz)), False


def _zone(tzid: str | None, fallback: tzinfo) -> tzinfo:
    if tzid:
        try:
            return ZoneInfo(tzid)
        except Exception:
            return fallback  # 未知时区按配置时区处理
    return fallback


def _build_events(
    block: dict[str, tuple[str, dict[str, str]]], tz: tzinfo, horizon_end: datetime | None
) -> list[Event]:
    """把一个 VEVENT 展开成事件列表（含 RRULE 的重复展开）。"""
    if "DTSTART" not in block:
        return []  # 没有开始时间的事件无法播报
    raw_start = block["DTSTART"]
    try:
        start, all_day = _parse_dt(raw_start[0], raw_start[1], tz)
    except ValueError:
        return []  # 时间格式非法的事件直接跳过，不影响其他日程
    summary = block.get("SUMMARY", ("（未命名）", {}))[0].strip() or "（未命名）"
    location = block.get("LOCATION", ("", {}))[0].strip() or None
    end = None
    if "DTEND" in block:
        raw_end = block["DTEND"]
        try:
            end, _ = _parse_dt(raw_end[0], raw_end[1], tz)
        except ValueError:
            end = None
    rule = block.get("RRULE", ("", {}))[0]
    if not rule:
        return [Event(start=start, end=end, summary=summary, all_day=all_day, location=location)]
    duration = (end - start) if end else None
    return [
        Event(
            start=moment,
            end=(moment + duration) if duration else None,
            summary=summary,
            all_day=all_day,
            location=location,
        )
        for moment in _occurrences(start, rule, horizon_end)
    ]


def _occurrences(start: datetime, rule: str, horizon_end: datetime | None) -> list[datetime]:
    """展开 RRULE 的重复时刻；只支持 DAILY / WEEKLY（其余按单次处理）。"""
    parts = dict(item.split("=", 1) for item in rule.split(";") if "=" in item)
    freq = parts.get("FREQ", "").upper()
    if freq not in ("DAILY", "WEEKLY"):
        return [start]
    interval = max(1, int(parts.get("INTERVAL", 1)))
    limit = horizon_end or (start + timedelta(days=366))
    found: list[datetime] = []
    if freq == "DAILY":
        cursor = start
        for _ in range(400):
            if cursor > limit:
                break
            found.append(cursor)
            cursor += timedelta(days=interval)
    else:
        weekdays = _weekdays(parts.get("BYDAY")) or [start.weekday()]
        monday = (start - timedelta(days=start.weekday())).replace(
            hour=start.hour, minute=start.minute, second=start.second
        )
        cursor = monday
        for _ in range(400):
            if cursor > limit:
                break
            for weekday in sorted(weekdays):
                moment = cursor + timedelta(days=weekday)
                if moment >= start:
                    found.append(moment)
            cursor += timedelta(weeks=interval)
    if parts.get("COUNT"):
        found = found[: int(parts["COUNT"])]
    if parts.get("UNTIL"):
        try:
            until, _ = _parse_dt(parts["UNTIL"], {}, start.tzinfo or UTC)
        except ValueError:
            until = None  # UNTIL 非法时忽略该条件，好过丢弃整个事件
        if until is not None:
            found = [moment for moment in found if moment <= until]
    return found


def _weekdays(raw: str | None) -> list[int]:
    mapping = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
    if not raw:
        return []
    return [mapping[item[-2:]] for item in raw.split(",") if item[-2:] in mapping]


class CalendarService:
    """按需读取日程；``fetch_text`` 与 ``now_fn`` 可注入，便于离线单测。"""

    def __init__(
        self,
        cfg: dict[str, Any],
        *,
        fetch_text: FetchText | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = cfg.get("ics_path") or None
        self.url = cfg.get("ics_url") or None
        self.days = int(cfg.get("days", 2))
        self.lookahead_days = int(cfg.get("lookahead_days", 60))
        self.timeout_s = float(cfg.get("timeout_s", 10.0))
        self.tz = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))
        self._fetch_text = fetch_text or self._http_text
        self._now = now_fn or (lambda: datetime.now(self.tz))

    async def _http_text(self, url: str) -> str:
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text

    async def _load(self) -> str | None:
        chunks: list[str] = []
        if self.path:
            try:
                chunks.append(Path(self.path).read_text(encoding="utf-8"))
            except OSError:
                pass  # 文件不存在/不可读：当作"没有日程"
        if self.url:
            try:
                chunks.append(await self._fetch_text(self.url))
            except Exception:
                pass  # 订阅拉取失败：静默降级
        return "\n".join(chunks) or None

    async def events(self, now: datetime | None = None, days: int | None = None) -> list[Event]:
        """返回"当前仍在进行 + 未来 N 天内开始"的事件。"""
        raw = await self._load()
        if not raw:
            return []
        moment = now or self._now()
        span = self.days if days is None else days
        window_end = (moment + timedelta(days=span)).replace(
            hour=23, minute=59, second=59, microsecond=0
        )
        horizon = max(moment + timedelta(days=self.lookahead_days), window_end)
        picked = [
            event
            for event in parse_ics(raw, tz=self.tz, horizon_end=horizon)
            if event.start <= window_end and _ends_after(event, moment)
        ]
        return picked

    async def report_text(self, now: datetime | None = None) -> str | None:
        """口语化播报；没有安排或取不到内容时返回 None（不占话）。"""
        moment = now or self._now()
        picked = await self.events(moment)
        return format_schedule(picked, moment) if picked else None


def _ends_after(event: Event, moment: datetime) -> bool:
    """全天事件的结束按当天末刻算，避免当天全天事件被误判为已过期。"""
    if event.end is not None:
        return event.end >= moment
    if event.all_day:
        return (event.start + timedelta(days=1)) > moment
    return event.start >= moment


def format_schedule(events: list[Event], now: datetime) -> str:
    """按天分组组织成一句话，如"今天有 2 个安排：09:00 周会；14:30 牙医"。"""
    today = now.date()
    groups: dict[date, list[Event]] = {}
    for event in events:
        groups.setdefault(event.start.date(), []).append(event)
    chunks: list[str] = []
    for day in sorted(groups):
        items = [f"{event.when} {event.summary}" for event in groups[day]]
        if len(items) > MAX_ITEMS:
            items = [*items[:MAX_ITEMS], "等"]
        chunks.append(f"{_day_label(day, today)}有 {len(groups[day])} 个安排：{'；'.join(items)}")
    return "。".join(chunks) + "。"


def _day_label(day: date, today: date) -> str:
    delta = (day - today).days
    if delta <= 0:
        return "今天"
    if delta == 1:
        return "明天"
    return f"{day.month}月{day.day}日"
