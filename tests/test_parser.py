"""LLM 结构化输出解析单测：协议抽取、分块边界、转义与容错。"""

from __future__ import annotations

import unittest

from robot.brain.parser import OutputExtractor

JSON_REPLY = (
    '{"text":"你好呀，今天过得怎么样？","emotion":"happy",'
    '"intensity":0.5,"gesture":"tilt_head"}'
)


class TestOutputExtractor(unittest.TestCase):
    def test_extracts_text_and_meta(self) -> None:
        ex = OutputExtractor()
        text = ex.feed(JSON_REPLY) + ex.finish()
        self.assertEqual(text, "你好呀，今天过得怎么样？")
        self.assertEqual(ex.meta["emotion"], "happy")
        self.assertEqual(ex.meta["gesture"], "tilt_head")
        self.assertEqual(ex.meta["intensity"], 0.5)

    def test_chunked_incremental_output(self) -> None:
        """逐字符喂入结果应与整块喂入一致（含跨 chunk 切断的 key）。"""
        for size in (1, 2, 3, 5, 7):
            with self.subTest(chunk_size=size):
                ex = OutputExtractor()
                out = "".join(
                    ex.feed(JSON_REPLY[i : i + size])
                    for i in range(0, len(JSON_REPLY), size)
                )
                out += ex.finish()
                self.assertEqual(out, "你好呀，今天过得怎么样？")
                self.assertEqual(ex.meta["emotion"], "happy")

    def test_plain_text_passthrough(self) -> None:
        """离线桩 / 兜底话术等非 JSON 输出，应原样透传。"""
        ex = OutputExtractor()
        text = ex.feed("嗯嗯，我在听~ ") + ex.feed("你继续说。") + ex.finish()
        self.assertEqual(text, "嗯嗯，我在听~ 你继续说。")
        self.assertEqual(ex.meta, {})

    def test_escapes_are_decoded(self) -> None:
        raw = '{"text":"他说\\"你好\\"\\n然后走了。","emotion":"neutral"}'
        ex = OutputExtractor()
        text = ex.feed(raw) + ex.finish()
        self.assertEqual(text, '他说"你好"\n然后走了。')

    def test_unicode_escape(self) -> None:
        ex = OutputExtractor()
        text = ex.feed('{"text":"\\u4f60\\u597d","emotion":"neutral"}') + ex.finish()
        self.assertEqual(text, "你好")

    def test_unterminated_text_is_tolerated(self) -> None:
        """模型漏掉收尾引号时，残留内容仍要念出来。"""
        ex = OutputExtractor()
        text = ex.feed('{"text":"今天辛苦了') + ex.finish()
        self.assertEqual(text, "今天辛苦了")

    def test_protocol_miss_falls_back_to_raw(self) -> None:
        """有 { 但没有 text 字段时，整体按纯文本朗读，避免机器人哑掉。"""
        ex = OutputExtractor()
        text = ex.feed('{"foo":"bar"}') + ex.finish()
        self.assertEqual(text, '{"foo":"bar"}')

    def test_empty_input(self) -> None:
        ex = OutputExtractor()
        self.assertEqual(ex.feed("") + ex.finish(), "")
        self.assertEqual(ex.meta, {})


if __name__ == "__main__":
    unittest.main()
