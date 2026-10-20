"""记忆模块：三层记忆（技术方案 §5.5）。

- 工作记忆：对话流水线内的最近 N 轮（pipeline.history）；
- 短期记忆：ShortTermMemory（SQLite，近 7 天对话摘要滚动）；
- 长期记忆：MemoryStore（SQLite 持久化）+ FactExtractor（每轮异步抽取事实）。

未启用记忆时使用 NullMemory，保证离线链路照常可跑。
"""

from __future__ import annotations

from .embedder import OpenAICompatEmbedder
from .extractor import FactExtractor, parse_facts
from .onnx_embedder import OnnxEmbedder
from .short_term import ShortTermMemory
from .store import MemoryStore


class NullMemory:
    """空记忆：永远返回空命中，保证链路可跑通。"""

    async def recall(self, query: str, k: int = 5) -> list[str]:
        return []

    async def remember(self, facts: list[dict]) -> None:
        return None


__all__ = [
    "FactExtractor",
    "MemoryStore",
    "NullMemory",
    "OnnxEmbedder",
    "OpenAICompatEmbedder",
    "ShortTermMemory",
    "parse_facts",
]
