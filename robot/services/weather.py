"""天气服务（M6，技术方案 §5.1 services/weather.py）。

用 Open-Meteo：免费、免 API Key，返回 JSON。
失败一律返回 ``None`` —— 主动服务取不到内容就保持安静，绝不硬凑（§7.5 不哑原则）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

API_URL = "https://api.open-meteo.com/v1/forecast"

# Open-Meteo 用 WMO 4677 天气编码，这里映射成中文口语描述
WMO_CODES: dict[int, str] = {
    0: "晴",
    1: "晴间多云",
    2: "多云",
    3: "阴",
    45: "有雾",
    48: "有雾凇",
    51: "毛毛雨",
    53: "小雨",
    55: "中雨",
    56: "冻毛毛雨",
    57: "冻雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴冰雹",
    99: "强雷暴伴冰雹",
}

FetchJson = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def describe_code(code: int | None) -> str:
    """WMO 编码 -> 中文描述；未知编码统一"天气不明"，不抛错。"""
    if code is None:
        return "天气不明"
    return WMO_CODES.get(code, "天气不明")


class WeatherService:
    """拉取当前天气与今日预报；``fetch`` 可注入，便于离线单测。"""

    def __init__(self, cfg: dict[str, Any], *, fetch: FetchJson | None = None) -> None:
        self.latitude = cfg.get("latitude")
        self.longitude = cfg.get("longitude")
        self.city = cfg.get("city", "")
        self.timezone = cfg.get("timezone", "Asia/Shanghai")
        self.timeout_s = float(cfg.get("timeout_s", 10.0))
        self._fetch = fetch or self._http_fetch

    async def _http_fetch(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()

    async def snapshot(self) -> dict[str, Any] | None:
        """拉取一次天气并整理成扁平字典；未配置经纬度或请求失败时返回 None。"""
        if self.latitude is None or self.longitude is None:
            return None
        params = {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "current": "temperature_2m,relative_humidity_2m,weather_code",
            "daily": (
                "weather_code,temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max"
            ),
            "timezone": self.timezone,
            "forecast_days": 1,
        }
        try:
            data = await self._fetch(API_URL, params)
        except Exception:
            return None  # 断网/超时/接口异常：静默降级，交给调用方决定不开口
        return self._parse(data)

    def _parse(self, data: dict[str, Any]) -> dict[str, Any] | None:
        current = data.get("current") or {}
        daily = data.get("daily") or {}
        temp = current.get("temperature_2m")
        if temp is None:
            return None  # 结构不符预期时按"取不到"处理
        return {
            "city": self.city,
            "temperature": round(float(temp)),
            "humidity": current.get("relative_humidity_2m"),
            "code": current.get("weather_code"),
            "text": describe_code(current.get("weather_code")),
            "high": _round_or_none(_first(daily.get("temperature_2m_max"))),
            "low": _round_or_none(_first(daily.get("temperature_2m_min"))),
            "rain_prob": _first(daily.get("precipitation_probability_max")),
        }

    async def report_text(self) -> str | None:
        """口语化播报，如"上海今天多云，13~22℃，降水概率 30%"。取不到返回 None。"""
        snap = await self.snapshot()
        return None if snap is None else format_report(snap)

    async def rain_alert_text(self, threshold: float = 60.0) -> str | None:
        """降水概率超阈值才提醒；否则返回 None（调度器据此保持安静）。"""
        snap = await self.snapshot()
        if snap is None or snap.get("rain_prob") is None:
            return None
        if float(snap["rain_prob"]) < threshold:
            return None
        return f"今天降水概率 {round(float(snap['rain_prob']))}%，出门记得带伞。"


def format_report(snap: dict[str, Any]) -> str:
    """把 snapshot 组织成一句播报文本。"""
    prefix = f"{snap['city']}今天" if snap.get("city") else "今天"
    text = f"{prefix}{snap['text']}"
    if snap.get("low") is not None and snap.get("high") is not None:
        text += f"，气温 {snap['low']}~{snap['high']}℃"
    else:
        text += f"，当前 {snap['temperature']}℃"
    if snap.get("rain_prob") is not None and float(snap["rain_prob"]) >= 30:
        text += f"，降水概率 {round(float(snap['rain_prob']))}%"
    return text + "。"


def _first(value: Any) -> Any:
    return value[0] if isinstance(value, list) and value else None


def _round_or_none(value: Any) -> int | None:
    return None if value is None else round(float(value))
