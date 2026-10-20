"""长期记忆存储：SQLite 持久化 + 相似度检索（技术方案 §5.5 / §7.3）。

三层记忆中的"长期记忆"由本模块承担：用户事实、偏好、重要事件，永久保存。

检索有两种模式，由是否注入 embedder 决定：

- **向量模式**（有 embedder）：余弦相似度，语义更准，需 embedding 服务额度；
- **词面模式**（无 embedder）：字符一元/二元组覆盖率，零依赖、离线可用。

技术方案 §6.5 本就建议"Embedding 端侧优先"，且桌面场景事实量小，
因此词面模式作为默认，向量模式作为可选升级。
两种分数量纲不同，``min_score`` 需按模式调。
"""

from __future__ import annotations

import array
import math
import sqlite3
from datetime import date
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    content    TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    confidence REAL NOT NULL,
    expire     TEXT,
    embedding  BLOB,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _grams(text: str) -> set[str]:
    """字符级一元组 + 二元组。不引入分词器时的常用近似，兼顾短查询与短语。"""
    clean = "".join(ch for ch in text if not ch.isspace())
    if not clean:
        return set()
    grams = set(clean)
    grams.update(clean[i : i + 2] for i in range(len(clean) - 1))
    return grams


def _lexical_score(query: str, target: str) -> float:
    """查询侧覆盖率 |A∩B| / |A|。中文不分词时比 Jaccard 稳：

    长问句不会因为目标短语短而被稀释到阈值以下（如"面试准备得怎么样"）。
    词面模式较粗，仅作离线兜底；向量模式更准（§6.5）。
    """
    a, b = _grams(query), _grams(target)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a)


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class MemoryStore:
    """SQLite 长期记忆。同一 content 再次写入即覆盖（冲突取最新，§7.3）。"""

    def __init__(
        self,
        db_path: str = "data/memory.db",
        embedder=None,
        top_k: int = 3,
        min_score: float = 0.25,
        min_confidence: float = 0.6,
    ) -> None:
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, isolation_level=None)
        self.conn.execute(_SCHEMA)
        self.embedder = embedder
        self.top_k = top_k
        self.min_score = min_score
        self.min_confidence = min_confidence

    async def recall(self, query: str, k: int | None = None, today: str | None = None) -> list[str]:
        """检索与当前话语最相关的记忆，返回内容列表（已过滤过期与低置信度）。"""
        scored = await self._score(query, today)
        limit = self.top_k if k is None else k
        return [content for content, score in scored if score >= self.min_score][:limit]

    async def remember(self, facts: list[dict]) -> None:
        """写入事实；content 相同则覆盖（取最新）。"""
        stamp = date.today().isoformat()
        for fact in facts:
            content = (fact.get("content") or "").strip()
            if not content:
                continue
            blob = None
            if self.embedder is not None:
                try:
                    vector = await self.embedder.embed(content)
                    blob = array.array("f", vector).tobytes()
                except Exception:
                    blob = None  # 向量服务不可用时，事实本身仍要存下来
            self.conn.execute(
                "INSERT INTO facts (content, kind, confidence, expire, embedding,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(content) DO UPDATE SET"
                " kind=excluded.kind, confidence=excluded.confidence,"
                " expire=excluded.expire, embedding=excluded.embedding,"
                " updated_at=excluded.updated_at",
                (
                    content,
                    fact.get("kind", "profile"),
                    float(fact.get("confidence", 0.0)),
                    fact.get("expire"),
                    blob,
                    stamp,
                    stamp,
                ),
            )

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]

    def all_facts(self) -> list[dict]:
        """列出全部长期事实（按更新时间倒序），供 /mem 调试查看。"""
        rows = self.conn.execute(
            "SELECT content, kind, confidence, expire, updated_at FROM facts"
            " ORDER BY updated_at DESC"
        ).fetchall()
        return [
            {
                "content": content,
                "kind": kind,
                "confidence": confidence,
                "expire": expire,
                "updated_at": updated_at,
            }
            for content, kind, confidence, expire, updated_at in rows
        ]

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #

    async def _score(self, query: str, today: str | None) -> list[tuple[str, float]]:
        today = today or date.today().isoformat()
        rows = self.conn.execute(
            "SELECT content, embedding FROM facts"
            " WHERE confidence >= ? AND (expire IS NULL OR expire >= ?)",
            (self.min_confidence, today),
        ).fetchall()

        query_vector: list[float] | None = None
        if self.embedder is not None:
            try:
                query_vector = await self.embedder.embed(query)
            except Exception:
                query_vector = None  # 向量服务不可用则整体退回词面检索（§7.5 不哑原则）

        scored: list[tuple[str, float]] = []
        for content, blob in rows:
            vector = _from_blob(blob) if query_vector is not None else []
            score = (
                _cosine(query_vector, vector)
                if vector
                else _lexical_score(query, content)
            )
            scored.append((content, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored


def _from_blob(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    values = array.array("f")
    values.frombytes(blob)
    return list(values)
