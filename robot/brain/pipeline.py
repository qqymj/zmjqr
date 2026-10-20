"""对话流水线：组装上下文 -> 流式调用 LLM -> 按句切分产出。

按句流式是首字延迟优化的关键（技术方案 §5.4 / §6.2）：
LLM 边生成边按标点切句，首句一到就交给 TTS。

模型返回的是 JSON 协议（见 persona.OUTPUT_PROTOCOL），由 OutputExtractor
流式剥离出可朗读文本，并顺带解析出 emotion/gesture 驱动表达层。
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

from ..core.eventbus import EventBus
from .parser import OutputExtractor

SENT_END = re.compile(r"[。！？!?\n]")


def split_sentences(buffer: str) -> tuple[list[str], str]:
    """按句末标点切分，返回 (完整句子列表, 残留缓冲)。"""
    sentences: list[str] = []
    while (match := SENT_END.search(buffer)) is not None:
        sentence, buffer = buffer[: match.end()], buffer[match.end() :]
        if sentence.strip():
            sentences.append(sentence.strip())
    return sentences, buffer


class DialoguePipeline:
    def __init__(self, bus: EventBus, llm, persona, emotion, memory, short_term=None) -> None:
        self.bus = bus
        self.llm = llm
        self.persona = persona
        self.emotion = emotion
        self.memory = memory
        self.short_term = short_term
        self.history: list[dict] = []

    async def run(self, user_text: str) -> AsyncIterator[str]:
        mood = self.emotion.detect(user_text)
        await self.bus.emit("emotion.detected", {"emotion": mood, "text": user_text})

        memory_hits = await self.memory.recall(user_text)
        if memory_hits:
            await self.bus.emit("memory.recalled", {"hits": memory_hits})
        recent = await self.short_term.recent() if self.short_term is not None else None
        messages = self.persona.build(user_text, self.history, memory_hits, mood, recent)

        extractor = OutputExtractor()
        collected: list[str] = []
        raw_parts: list[str] = []
        buffer = ""
        try:
            async for token in self.llm.stream(messages):
                raw_parts.append(token)
                piece = extractor.feed(token)
                if not piece:
                    continue
                buffer += piece
                sentences, buffer = split_sentences(buffer)
                for sentence in sentences:
                    collected.append(sentence)
                    yield sentence

            tail = extractor.finish()
            if tail:
                buffer += tail
            sentences, buffer = split_sentences(buffer)
            collected.extend(sentences)
            for sentence in sentences:
                yield sentence
            if buffer.strip():
                collected.append(buffer.strip())
                yield buffer.strip()

            if extractor.meta.get("emotion"):
                await self.bus.emit("expression.chosen", extractor.meta)
        finally:
            # 放在 finally：LLM 报错或流被提前打断时同样落地本轮，
            # 用户话语不能丢（技术方案 §7.5 不哑 / 记得住）。
            await self._remember(user_text, "".join(raw_parts), "".join(collected), mood)

    async def _remember(
        self, user_text: str, raw_reply: str, reply: str, mood: str
    ) -> None:
        """更新会话历史并广播，供记忆/埋点模块订阅。

        history 存模型**原始输出**（保留 JSON 协议）：若只存剥离后的朗读文本，
        模型看到自己上一轮是纯中文，就会模仿成纯文本、逐步丢掉 emotion 字段。
        utterance.completed 只带可朗读文本，供记忆与埋点使用。
        """
        self.history.append({"role": "user", "content": user_text})
        answer = raw_reply or reply
        if answer:  # LLM 失败时这一轮没有回答，不写入空 assistant 消息
            self.history.append({"role": "assistant", "content": answer})
        # 工作记忆只保留最近 10 轮，避免 Prompt 无限增长
        max_messages = 20
        if len(self.history) > max_messages:
            self.history = self.history[-max_messages:]
        if self.short_term is not None:
            # 短期记忆持久化本轮，供跨会话的"最近聊过什么"连续性
            await self.short_term.add(user_text, reply)
        if reply:  # 没有回答就没有抽取价值，避免空内容触发抽取调用
            await self.bus.emit(
                "utterance.completed", {"user": user_text, "reply": reply, "emotion": mood}
            )
