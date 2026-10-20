"""事实抽取：每轮对话后抽出值得长期记住的、关于用户的信息（技术方案 §5.5）。

订阅 ``utterance.completed``，在后台任务里调用 LLM 抽取，不占用对话延迟。
抽取出的事实写入长期记忆（MemoryStore）并广播 ``memory.stored``。
"""

from __future__ import annotations

import asyncio
import json
from datetime import date

from ..core.eventbus import EventBus

EXTRACT_PROMPT = """你在为陪伴机器人维护"长期记忆"：
从一轮对话中抽出值得长期记住的、关于用户的稳定信息。

类型 kind：
- preference  偏好（喜欢 / 讨厌什么）
- profile     个人信息（职业、作息、习惯、性格）
- event       有明确时间的安排（必须给 expire）
- relation    人际关系（家人、朋友、同事）

规则：
- 只记用户的信息，不记寒暄，也不记机器人自己说过的话
- 没有值得长期记的就返回 {"facts": []}
- content 用第三人称、简短陈述，不超过 20 字，例如"喜欢喝美式咖啡"
- event 必须给 expire，格式 YYYY-MM-DD；其余类型 expire 为 null
- confidence 取 0~1；拿不准就给低于 0.6 的值
- 只输出 JSON，不要解释

输出格式：{"facts":[{"kind":"preference","content":"...","confidence":0.9,"expire":null}]}"""


def _loads_object(raw: str) -> dict | None:
    """尽力从模型输出里取出一个 JSON 对象。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text
    for candidate in (text, text[text.find("{") : text.rfind("}") + 1]):
        if not candidate or "{" not in candidate:
            continue
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _clean_expire(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def parse_facts(raw: str, min_confidence: float = 0.6) -> list[dict]:
    """解析抽取结果，丢弃空内容与低置信度事实。"""
    payload = _loads_object(raw)
    if payload is None or not isinstance(payload.get("facts"), list):
        return []

    facts: list[dict] = []
    for item in payload["facts"]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if confidence < min_confidence:
            continue
        facts.append(
            {
                "content": content,
                "kind": str(item.get("kind") or "profile"),
                "confidence": confidence,
                "expire": _clean_expire(item.get("expire")),
            }
        )
    return facts


class FactExtractor:
    def __init__(
        self,
        bus: EventBus,
        llm,
        memory,
        min_confidence: float = 0.6,
    ) -> None:
        self.bus = bus
        self.llm = llm
        self.memory = memory
        self.min_confidence = min_confidence
        self._tasks: set[asyncio.Task] = set()
        bus.on("utterance.completed", self._on_completed)

    async def _on_completed(self, payload: dict) -> None:
        task = asyncio.create_task(self._extract(payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _extract(self, payload: dict) -> None:
        messages = [
            {"role": "system", "content": EXTRACT_PROMPT},
            {
                "role": "user",
                "content": f"用户：{payload['user']}\n小伴：{payload['reply']}",
            },
        ]
        raw = ""
        try:
            async for token in self.llm.stream(messages):
                raw += token
        except Exception as exc:  # 抽取失败不影响对话主链路
            await self.bus.emit(
                "memory.extract_failed", {"error": f"{type(exc).__name__}: {exc}"}
            )
            return

        facts = parse_facts(raw, self.min_confidence)
        if not facts:
            return
        await self.memory.remember(facts)
        await self.bus.emit(
            "memory.stored",
            {"facts": [f"{f['content']}({f['kind']})" for f in facts]},
        )

    async def aclose(self) -> None:
        """等待后台抽取任务结束，避免退出时丢事实。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
