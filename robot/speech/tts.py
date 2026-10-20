"""语音合成（M0 用终端桩实现）。

M2 替换为 ConsoleTTS -> CosyVoiceTTS / EdgeTTS，保持 play() 接口不变。
``bus`` 可选：传入后会广播 ``tts.playing``，供表达层把动作与说话时间轴对齐
（技术方案 §9.1 / §5.8）。
"""

from __future__ import annotations

import asyncio


class ConsoleTTS:
    def __init__(
        self,
        speaker: str = "小伴",
        per_char_delay: float = 0.02,
        bus=None,
    ) -> None:
        self.speaker = speaker
        self.per_char_delay = per_char_delay
        self.bus = bus

    async def play(self, sentence: str) -> None:
        """“播放”一句话：M0 在终端打印，用时模拟播报耗时。"""
        if self.bus is not None:
            await self.bus.emit("tts.playing", {"text": sentence, "speaking": True})
        print(f"[{self.speaker}] {sentence}")
        if self.per_char_delay:
            await asyncio.sleep(min(len(sentence) * self.per_char_delay, 1.5))
