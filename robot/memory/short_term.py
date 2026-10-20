"""短期记忆：近 N 天对话摘要，SQLite 滚动（技术方案 §5.5）。

工作记忆（``pipeline.history``）只在单次会话内有效，进程一重启就清空；
短期记忆把"最近聊过什么"持久化下来，给跨会话提供连续性。

- **滚动**：每轮对话追加一条，写入时顺带清理超过 ``days`` 天的旧记录；
- **离线可用**：不依赖 LLM，纯本地 SQLite，写入为微秒级同步操作；
- **注入**：按时间取最近 ``inject_limit`` 条（短期记忆不需要相似度检索，
  §5.5 的向量检索针对的是长期记忆）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_term (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    user_text  TEXT NOT NULL,
    reply_text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_short_term_ts ON short_term(ts);
"""


def _clip(text: str, max_chars: int) -> str:
    """压掉空白并截断，避免单条记录把 Prompt 撑长。"""
    clean = " ".join(text.split())
    return clean if len(clean) <= max_chars else clean[:max_chars] + "…"


def _format(ts: str, user_text: str, reply_text: str, max_chars: int) -> str:
    """格式化为机器人视角的一行摘要：``MM-DD 你说「…」，我答「…」``。

    LLM 失败的那一轮没有回答，只留用户话语，避免出现空的「我答「」」。
    """
    day = ts[5:10]  # ISO "2026-10-19T…" -> "10-19"
    said = f"你说「{_clip(user_text, max_chars)}」"
    if not reply_text.strip():
        return f"{day} {said}"
    return f"{day} {said}，我答「{_clip(reply_text, max_chars)}」"


class ShortTermMemory:
    def __init__(
        self,
        db_path: str = "data/short_term.db",
        days: int = 7,
        inject_limit: int = 5,
        max_chars: int = 40,
    ) -> None:
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, isolation_level=None)
        self.conn.executescript(_SCHEMA)
        self.days = days
        self.inject_limit = inject_limit
        self.max_chars = max_chars

    async def add(
        self, user_text: str, reply_text: str, now: datetime | None = None
    ) -> None:
        """追加一轮对话并滚动清理过期记录。"""
        now = now or datetime.now()
        self.conn.execute(
            "INSERT INTO short_term (ts, user_text, reply_text) VALUES (?, ?, ?)",
            (now.isoformat(timespec="seconds"), user_text, reply_text),
        )
        self.purge(now)

    async def recent(self, limit: int | None = None) -> list[str]:
        """取最近若干条摘要，按时间正序返回（便于按对话顺序阅读）。"""
        limit = self.inject_limit if limit is None else limit
        rows = self.conn.execute(
            "SELECT ts, user_text, reply_text FROM short_term ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_format(ts, u, r, self.max_chars) for ts, u, r in reversed(rows)]

    def purge(self, now: datetime | None = None) -> int:
        """删除超过 days 天的记录，返回删除条数。"""
        cutoff = (now or datetime.now()) - timedelta(days=self.days)
        cursor = self.conn.execute(
            "DELETE FROM short_term WHERE ts < ?",
            (cutoff.isoformat(timespec="seconds"),),
        )
        return cursor.rowcount

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM short_term").fetchone()[0]

    def close(self) -> None:
        self.conn.close()
