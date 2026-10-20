"""长期记忆单测：事实解析、SQLite 存取、双模式检索与降级。"""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from robot.brain.persona import Persona
from robot.brain.pipeline import DialoguePipeline
from robot.core.eventbus import EventBus
from robot.emotion.detector import EmotionDetector
from robot.main import _memory_report
from robot.memory import (
    FactExtractor,
    MemoryStore,
    NullMemory,
    ShortTermMemory,
    parse_facts,
)
from robot.memory.onnx_embedder import OnnxEmbedder, _cls_pool_normalize

try:
    import numpy  # noqa: F401

    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

try:
    import onnxruntime  # noqa: F401
    import tokenizers  # noqa: F401

    _HAS_RUNTIME = True
except ImportError:
    _HAS_RUNTIME = False

_MODEL_DIR = Path(__file__).resolve().parents[1] / "data/models/bge-small-zh-v1.5"
_HAS_MODEL = (_MODEL_DIR / "tokenizer.json").exists() and (
    _MODEL_DIR / "onnx" / "model_quantized.onnx"
).exists()

_LONG_AGO = (date.today() - timedelta(days=30)).isoformat()


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


class _FakeEmbedder:
    """确定性假向量：按关键词命中返回方向向量，用于验证向量检索路径。"""

    DIRECTIONS = {"咖啡": [1.0, 0.0], "猫": [0.0, 1.0]}

    async def embed(self, text: str) -> list[float]:
        for word, direction in self.DIRECTIONS.items():
            if word in text:
                return direction
        return [0.1, 0.1]


class _FailingEmbedder:
    async def embed(self, _text: str) -> list[float]:
        raise RuntimeError("embedding 服务不可用")


class _StubLLM:
    """按固定内容流式返回的假 LLM。"""

    name = "stub"

    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def stream(self, _messages):
        for char in self.reply:
            yield char


class TestParseFacts(unittest.TestCase):
    def test_keeps_valid_facts(self) -> None:
        raw = json.dumps(
            {
                "facts": [
                    {"kind": "preference", "content": "喜欢喝美式咖啡", "confidence": 0.9},
                    {
                        "kind": "event",
                        "content": "10月20日有面试",
                        "confidence": 0.95,
                        "expire": "2026-10-21",
                    },
                ]
            }
        )
        facts = parse_facts(raw)
        self.assertEqual([f["content"] for f in facts], ["喜欢喝美式咖啡", "10月20日有面试"])
        self.assertEqual(facts[1]["expire"], "2026-10-21")
        self.assertIsNone(facts[0]["expire"])

    def test_drops_low_confidence_and_empty(self) -> None:
        raw = json.dumps(
            {
                "facts": [
                    {"content": "随口一提", "confidence": 0.3},
                    {"content": "   ", "confidence": 0.9},
                    {"content": "确定的事", "confidence": 0.8},
                ]
            }
        )
        self.assertEqual([f["content"] for f in parse_facts(raw)], ["确定的事"])

    def test_tolerates_code_fence_and_prose(self) -> None:
        raw = (
            '好的，结果如下：\n```json\n'
            '{"facts":[{"content":"在互联网公司做产品","confidence":0.7}]}\n```'
        )
        facts = parse_facts(raw)
        self.assertEqual([f["content"] for f in facts], ["在互联网公司做产品"])
        self.assertEqual(facts[0]["kind"], "profile")

    def test_invalid_shapes_return_empty(self) -> None:
        for raw in ("", "没有值得记的内容", '{"facts":"不是列表"}', '{"other":[]}'):
            with self.subTest(raw=raw):
                self.assertEqual(parse_facts(raw), [])

    def test_invalid_expire_is_dropped(self) -> None:
        raw = json.dumps(
            {"facts": [{"content": "下周去体检", "confidence": 0.9, "expire": "下周"}]}
        )
        self.assertIsNone(parse_facts(raw)[0]["expire"])


class TestMemoryStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = MemoryStore(db_path=":memory:", min_score=0.15)

    async def asyncTearDown(self) -> None:
        self.store.close()

    async def test_remember_then_recall_relevant(self) -> None:
        await self.store.remember(
            [
                {"content": "喜欢喝美式咖啡", "confidence": 0.9},
                {"content": "养了一只叫豆豆的猫", "confidence": 0.9},
            ]
        )
        hits = await self.store.recall("今天想喝咖啡")
        self.assertEqual(hits, ["喜欢喝美式咖啡"])

    async def test_irrelevant_query_returns_nothing(self) -> None:
        await self.store.remember([{"content": "喜欢喝美式咖啡", "confidence": 0.9}])
        self.assertEqual(await self.store.recall("明天要交季度报告"), [])

    async def test_same_content_overwrites(self) -> None:
        await self.store.remember([{"content": "住在北京", "confidence": 0.7}])
        await self.store.remember([{"content": "住在北京", "confidence": 0.95}])
        self.assertEqual(self.store.count(), 1)

    async def test_expired_fact_not_recalled(self) -> None:
        await self.store.remember(
            [{"content": "明天有面试", "confidence": 0.9, "expire": _LONG_AGO}]
        )
        self.assertEqual(await self.store.recall("面试怎么样"), [])

    async def test_future_expire_is_recalled(self) -> None:
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        await self.store.remember(
            [{"content": "明天有面试", "confidence": 0.9, "expire": tomorrow}]
        )
        self.assertEqual(await self.store.recall("面试准备得怎么样"), ["明天有面试"])

    async def test_low_confidence_fact_not_recalled(self) -> None:
        await self.store.remember([{"content": "喜欢喝美式咖啡", "confidence": 0.3}])
        self.assertEqual(await self.store.recall("想喝咖啡"), [])

    async def test_top_k_limits_hits(self) -> None:
        store = MemoryStore(db_path=":memory:", min_score=0.05, top_k=2)
        try:
            await store.remember(
                [
                    {"content": "喜欢喝美式咖啡", "confidence": 0.9},
                    {"content": "也喜欢喝拿铁咖啡", "confidence": 0.9},
                    {"content": "咖啡因过敏", "confidence": 0.9},
                ]
            )
            self.assertEqual(len(await store.recall("想喝咖啡")), 2)
        finally:
            store.close()

    async def test_all_facts_lists_metadata(self) -> None:
        await self.store.remember(
            [{"content": "喜欢喝美式咖啡", "kind": "preference", "confidence": 0.9}]
        )
        facts = self.store.all_facts()
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["kind"], "preference")
        self.assertAlmostEqual(facts[0]["confidence"], 0.9)


class TestMemoryStoreVector(unittest.IsolatedAsyncioTestCase):
    async def test_vector_prefers_semantic_match(self) -> None:
        store = MemoryStore(db_path=":memory:", embedder=_FakeEmbedder(), min_score=0.6)
        try:
            await store.remember(
                [
                    {"content": "喜欢喝美式咖啡", "confidence": 0.9},
                    {"content": "养了一只叫豆豆的猫", "confidence": 0.9},
                ]
            )
            # 向量模式下"咖啡"方向命中，"猫"那条因余弦为 0 被阈值滤掉
            self.assertEqual(await store.recall("想喝咖啡"), ["喜欢喝美式咖啡"])
        finally:
            store.close()

    async def test_embedder_failure_falls_back_to_lexical(self) -> None:
        """向量服务挂掉时退回词面检索，存储与召回都不能哑（§7.5）。"""
        store = MemoryStore(db_path=":memory:", embedder=_FailingEmbedder(), min_score=0.15)
        try:
            await store.remember([{"content": "喜欢喝美式咖啡", "confidence": 0.9}])
            self.assertEqual(store.count(), 1)  # 事实本身仍存下来了
            self.assertEqual(await store.recall("今天想喝咖啡"), ["喜欢喝美式咖啡"])
        finally:
            store.close()


class TestFactExtractor(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_and_stores_facts(self) -> None:
        bus = EventBus()
        memory = MemoryStore(db_path=":memory:", min_score=0.15)
        reply = json.dumps(
            {"facts": [{"kind": "preference", "content": "喜欢喝美式咖啡", "confidence": 0.9}]}
        )
        extractor = FactExtractor(bus, _StubLLM(reply), memory)

        stored: list[dict] = []

        async def on_stored(payload: dict) -> None:
            stored.append(payload)

        bus.on("memory.stored", on_stored)
        try:
            await extractor._extract({"user": "我平时爱喝美式", "reply": "记下啦"})
            self.assertEqual(memory.count(), 1)
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]["facts"], ["喜欢喝美式咖啡(preference)"])
        finally:
            memory.close()

    async def test_no_facts_leaves_memory_untouched(self) -> None:
        bus = EventBus()
        memory = MemoryStore(db_path=":memory:")
        extractor = FactExtractor(bus, _StubLLM('{"facts":[]}'), memory)
        try:
            await extractor._extract({"user": "你好", "reply": "你好呀"})
            self.assertEqual(memory.count(), 0)
        finally:
            memory.close()

    async def test_llm_failure_emits_event_without_raising(self) -> None:
        class Boom:
            name = "boom"

            async def stream(self, _messages):
                raise RuntimeError("网络断了")
                yield ""  # pragma: no cover

        bus = EventBus()
        memory = MemoryStore(db_path=":memory:")
        extractor = FactExtractor(bus, Boom(), memory)
        failed: list[dict] = []

        async def on_failed(payload: dict) -> None:
            failed.append(payload)

        bus.on("memory.extract_failed", on_failed)
        try:
            await extractor._extract({"user": "你好", "reply": "你好呀"})
            self.assertEqual(len(failed), 1)
            self.assertEqual(memory.count(), 0)
        finally:
            memory.close()


class _StubTokenizer:
    def enable_truncation(self, max_length: int = 512) -> None:
        self.max_length = max_length

    def encode(self, _text: str):
        encoding = type("Encoding", (), {})()
        encoding.ids = [101, 2769, 102]
        encoding.attention_mask = [1, 1, 1]
        encoding.type_ids = [0, 0, 0]
        return encoding


class _StubSession:
    """返回 [output][batch][seq][hidden]；仅首 token（CLS）行非零，验证池化取首位。"""

    def run(self, _outputs, _feeds):
        return [[[[3.0, 4.0, 0.0], [9.0, 9.0, 9.0]]]]


class TestOnnxEmbedderUnit(unittest.TestCase):
    def test_cls_pool_normalize(self) -> None:
        self.assertEqual(_cls_pool_normalize([3.0, 4.0, 0.0]), [0.6, 0.8, 0.0])
        self.assertEqual(_cls_pool_normalize([0.0, 0.0]), [0.0, 0.0])  # 零向量不除零

    @unittest.skipUnless(_HAS_NUMPY, "需要 numpy 构造 ONNX 入参")
    def test_embed_uses_cls_and_normalizes(self) -> None:
        embedder = OnnxEmbedder("unused", session=_StubSession(), tokenizer=_StubTokenizer())
        vector = embedder.embed_sync("随便一句")
        self.assertEqual(len(vector), 3)
        self.assertAlmostEqual(_cos(vector, vector), 1.0, places=6)  # 已 L2 归一化

    def test_missing_files_raise_runtime_error(self) -> None:
        with self.assertRaises(RuntimeError):
            OnnxEmbedder("/definitely/missing/model/dir")


@unittest.skipUnless(_HAS_RUNTIME and _HAS_MODEL, "端侧运行时或模型文件不可用")
class TestOnnxEmbedderReal(unittest.IsolatedAsyncioTestCase):
    async def test_real_bge_semantics(self) -> None:
        embedder = OnnxEmbedder(str(_MODEL_DIR))
        try:
            query = await embedder.embed("我平时喜欢喝什么咖啡")
            related = await embedder.embed("喜欢喝美式咖啡")
            unrelated = await embedder.embed("明天上午要去公司面试")
            self.assertEqual(len(query), 512)
            self.assertGreater(_cos(query, related), _cos(query, unrelated))
            self.assertGreater(_cos(query, related), 0.4)  # 超过向量模式阈值
        finally:
            await embedder.aclose()


class TestShortTermMemory(unittest.IsolatedAsyncioTestCase):
    async def test_recent_returns_chronological(self) -> None:
        st = ShortTermMemory(db_path=":memory:")
        try:
            await st.add("今天面试好紧张", "别慌，你准备得很充分")
            await st.add("面试过了", "太好了，恭喜你！")
            hits = await st.recent()
            self.assertEqual(len(hits), 2)
            self.assertIn("面试好紧张", hits[0])  # 早的在前
            self.assertIn("面试过了", hits[1])
            self.assertIn("我答「", hits[1])
        finally:
            st.close()

    async def test_purge_rolls_out_old_records(self) -> None:
        st = ShortTermMemory(db_path=":memory:", days=7)
        try:
            await st.add("八天前聊过的事", "嗯", now=datetime.now() - timedelta(days=8))
            await st.add("今天聊的事", "嗯")
            self.assertEqual(st.count(), 1)  # add 时已滚动清理过期记录
        finally:
            st.close()

    async def test_records_within_window_are_kept(self) -> None:
        st = ShortTermMemory(db_path=":memory:", days=7)
        try:
            await st.add("三天前聊过的事", "嗯", now=datetime.now() - timedelta(days=3))
            await st.add("今天聊的事", "嗯")
            self.assertEqual(st.count(), 2)
        finally:
            st.close()

    async def test_inject_limit_keeps_most_recent(self) -> None:
        st = ShortTermMemory(db_path=":memory:", inject_limit=2)
        try:
            for i in range(4):
                await st.add(f"话题{i}", f"回应{i}")
            hits = await st.recent()
            self.assertEqual(len(hits), 2)
            self.assertIn("话题2", hits[0])
            self.assertIn("话题3", hits[1])
        finally:
            st.close()

    async def test_long_text_is_clipped(self) -> None:
        st = ShortTermMemory(db_path=":memory:", max_chars=5)
        try:
            await st.add("一二三四五六七八九十", "好")
            self.assertIn("一二三四五…", (await st.recent())[0])
        finally:
            st.close()

    async def test_empty_reply_omits_answer_part(self) -> None:
        st = ShortTermMemory(db_path=":memory:")
        try:
            await st.add("今天好累", "")
            line = (await st.recent())[0]
            self.assertIn("今天好累", line)
            self.assertNotIn("我答", line)  # LLM 失败轮不出现空的「我答「」」
        finally:
            st.close()


class _BoomLLM:
    """模拟 LLM 调用失败（如断网、超时、无 Key）。"""

    async def stream(self, _messages):
        raise RuntimeError("llm down")
        yield  # pragma: no cover  仅用于标记这是异步生成器


class TestPipelineRecordsOnFailure(unittest.IsolatedAsyncioTestCase):
    async def test_utterance_kept_when_llm_fails(self) -> None:
        short_term = ShortTermMemory(db_path=":memory:")
        pipeline = DialoguePipeline(
            EventBus(),
            _BoomLLM(),
            Persona({"name": "小伴"}),
            EmotionDetector(),
            NullMemory(),
            short_term,
        )
        with self.assertRaises(RuntimeError):  # 异常照常上抛，交给上层播兜底话术
            async for _ in pipeline.run("今天好累"):
                pass
        self.assertEqual(short_term.count(), 1)  # 用户话语没有丢
        self.assertIn("今天好累", (await short_term.recent())[0])
        self.assertEqual(pipeline.history[0]["content"], "今天好累")
        self.assertEqual(len(pipeline.history), 1)  # 失败轮不写入空 assistant 消息
        short_term.close()

    async def test_normal_path_records_answer(self) -> None:
        class _OkLLM:
            async def stream(self, _messages):
                yield '{"text":"我在呢。","emotion":"happy","intensity":0.5,"gesture":"nod"}'

        short_term = ShortTermMemory(db_path=":memory:")
        pipeline = DialoguePipeline(
            EventBus(),
            _OkLLM(),
            Persona({"name": "小伴"}),
            EmotionDetector(),
            NullMemory(),
            short_term,
        )
        async for _ in pipeline.run("在吗"):
            pass
        self.assertEqual(short_term.count(), 1)
        self.assertIn("我答「我在呢。」", (await short_term.recent())[0])
        self.assertEqual(len(pipeline.history), 2)
        short_term.close()


class TestPersonaInjectsRecent(unittest.TestCase):
    def test_recent_dialogue_appended_to_system(self) -> None:
        persona = Persona({"name": "小伴"})
        messages = persona.build(
            "你好", [], [], "neutral", recent=["10-19 你说「面试紧张」，我答「别慌」"]
        )
        self.assertIn("最近几天你们聊过", messages[0]["content"])

    def test_no_recent_keeps_system_unchanged(self) -> None:
        persona = Persona({"name": "小伴"})
        messages = persona.build("你好", [], [], "neutral")
        self.assertNotIn("最近几天你们聊过", messages[0]["content"])


class TestMemoryReport(unittest.IsolatedAsyncioTestCase):
    async def test_report_covers_three_layers(self) -> None:
        memory = MemoryStore(db_path=":memory:")
        short_term = ShortTermMemory(db_path=":memory:")
        try:
            await memory.remember(
                [{"content": "喜欢喝美式咖啡", "kind": "preference", "confidence": 0.9}]
            )
            await short_term.add("明天面试好紧张", "别慌，你准备得很充分")
            history = [
                {"role": "user", "content": "你好"},
                {
                    "role": "assistant",
                    "content": '{"text":"我在呢","emotion":"neutral",'
                    '"intensity":0.5,"gesture":"still"}',
                },
            ]
            report = await _memory_report(history, memory, short_term)
            self.assertIn("[工作记忆]", report)
            self.assertIn("我在呢", report)  # JSON 协议被还原成可读文本
            self.assertIn("[短期记忆]", report)
            self.assertIn("明天面试好紧张", report)
            self.assertIn("[长期记忆]", report)
            self.assertIn("喜欢喝美式咖啡", report)
            self.assertIn("置信度 0.90", report)
        finally:
            memory.close()
            short_term.close()

    async def test_report_when_memory_disabled(self) -> None:
        report = await _memory_report([], NullMemory(), None)
        self.assertIn("[工作记忆] 当前会话最近 0 轮", report)
        self.assertIn("[短期记忆] 未启用", report)
        self.assertIn("[长期记忆] 未启用", report)


if __name__ == "__main__":
    unittest.main()
