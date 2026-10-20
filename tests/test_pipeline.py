"""对话流水线单测：验证按句切分、情绪事件与历史更新。"""

from __future__ import annotations

import unittest

from robot.brain.llm import EchoLLM
from robot.brain.persona import Persona
from robot.brain.pipeline import DialoguePipeline
from robot.core.eventbus import EventBus
from robot.emotion.detector import EmotionDetector
from robot.memory import NullMemory


def _make_pipeline() -> tuple[EventBus, DialoguePipeline]:
    bus = EventBus()
    pipeline = DialoguePipeline(
        bus=bus,
        llm=EchoLLM(char_delay=0),
        persona=Persona({"name": "小伴"}),
        emotion=EmotionDetector(),
        memory=NullMemory(),
    )
    return bus, pipeline


class _ProtocolLLM:
    """按角色卡 JSON 协议逐字符输出的假 LLM，用于验证结构化解析链路。"""

    name = "protocol-stub"

    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def stream(self, _messages):
        for char in self.reply:
            yield char


class TestDialoguePipeline(unittest.IsolatedAsyncioTestCase):
    async def test_yields_one_sentence_at_a_time(self) -> None:
        _bus, pipeline = _make_pipeline()
        sentences = []
        async for s in pipeline.run("今天有点累"):
            sentences.append(s)
        self.assertGreaterEqual(len(sentences), 2)
        self.assertTrue(all(s.endswith(("。", "？", "！")) for s in sentences))

    async def test_emits_emotion_event(self) -> None:
        bus, pipeline = _make_pipeline()
        moods: list[str] = []

        async def on_mood(payload: dict) -> None:
            moods.append(payload["emotion"])

        bus.on("emotion.detected", on_mood)
        async for _ in pipeline.run("今天有点累"):
            pass
        self.assertEqual(moods, ["sad"])

    async def test_updates_history_and_emits_completed(self) -> None:
        bus, pipeline = _make_pipeline()
        completed: list[dict] = []

        async def on_completed(payload: dict) -> None:
            completed.append(payload)

        bus.on("utterance.completed", on_completed)
        async for _ in pipeline.run("你好"):
            pass

        self.assertEqual(len(pipeline.history), 2)
        self.assertEqual(pipeline.history[0]["role"], "user")
        self.assertEqual(pipeline.history[1]["role"], "assistant")
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["emotion"], "neutral")

    async def test_parses_json_protocol_and_emits_expression(self) -> None:
        """模型按 JSON 协议返回时：只朗读 text，并把情绪/动作交给表达层。"""
        bus, _ = _make_pipeline()
        reply = (
            '{"text":"你好呀。今天过得怎么样？","emotion":"happy",'
            '"intensity":0.5,"gesture":"tilt_head"}'
        )
        pipeline = DialoguePipeline(
            bus=bus,
            llm=_ProtocolLLM(reply),
            persona=Persona({"name": "小伴"}),
            emotion=EmotionDetector(),
            memory=NullMemory(),
        )
        chosen: list[dict] = []
        completed: list[dict] = []

        async def on_chosen(payload: dict) -> None:
            chosen.append(payload)

        async def on_completed(payload: dict) -> None:
            completed.append(payload)

        bus.on("expression.chosen", on_chosen)
        bus.on("utterance.completed", on_completed)

        sentences = [s async for s in pipeline.run("你好")]
        self.assertEqual(sentences, ["你好呀。", "今天过得怎么样？"])
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0]["emotion"], "happy")
        self.assertEqual(chosen[0]["gesture"], "tilt_head")
        # history 保留模型原始输出（含 JSON 协议），作为后续轮次的格式范例
        self.assertIn('"emotion":"happy"', pipeline.history[1]["content"])
        # 而 utterance.completed 只带可朗读文本，不含 JSON 包装
        self.assertEqual(completed[0]["reply"], "你好呀。今天过得怎么样？")


if __name__ == "__main__":
    unittest.main()
