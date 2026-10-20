"""资讯服务（M6，技术方案 §5.1 services/news.py）。

聚合 RSS / Atom 订阅：免 API Key，也免第三方库（只用标准库 xml.etree 解析）。
- 每个源只取前几条标题，合并去重后按上限截断，避免一开口念一长串；
- 单个源拉取失败、返回的不是 XML（有些站点已撤掉 RSS，直接回 HTML）、
  或 XML 不合法，都只影响该源，其余照常；
- 全部源都取不到内容时返回 ``None``，调度器据此保持安静（不哑原则）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from xml.etree import ElementTree

import httpx

TITLE_LIMIT = 80  # 单条标题截断长度，控制播报时长
FetchText = Callable[[str], Awaitable[str]]


def parse_feed(raw: str, limit: int = 3) -> list[str]:
    """从 RSS（item）或 Atom（entry）文本里取前若干条标题；非法内容返回空列表。"""
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return []
    items = root.findall(".//item") or root.findall(".//{*}entry")
    titles: list[str] = []
    for item in items[:limit]:
        node = item.find("title")
        if node is None:
            node = item.find("{*}title")
        text = (node.text or "").strip() if node is not None else ""
        if text:
            titles.append(text[:TITLE_LIMIT])
    return titles


class NewsService:
    """按配置聚合订阅源；``fetch_text`` 可注入，便于离线单测。"""

    def __init__(self, cfg: dict[str, Any], *, fetch_text: FetchText | None = None) -> None:
        self.feeds = [feed for feed in cfg.get("feeds", []) if feed]
        self.per_feed = int(cfg.get("per_feed", 1))
        self.max_items = int(cfg.get("max_items", 3))
        self.timeout_s = float(cfg.get("timeout_s", 8.0))
        self._fetch_text = fetch_text or self._http_text

    async def _http_text(self, url: str) -> str:
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text

    async def _load(self, url: str) -> str | None:
        try:
            return await self._fetch_text(url)
        except Exception:
            return None  # 单个源失败：静默跳过，不影响其他源

    async def headlines(self) -> list[str]:
        """按配置顺序汇总各源标题，去重后截断到上限。"""
        found: list[str] = []
        for url in self.feeds:
            raw = await self._load(url)
            if raw:
                found.extend(parse_feed(raw, limit=self.per_feed))
        unique: list[str] = []
        seen: set[str] = set()
        for title in found:
            if title not in seen:
                seen.add(title)
                unique.append(title)
        return unique[: self.max_items]

    async def report_text(self) -> str | None:
        """口语化播报；一条都取不到时返回 None（不占话）。"""
        items = await self.headlines()
        if not items:
            return None
        return f"刚看到几条消息：{'；'.join(items)}。"
