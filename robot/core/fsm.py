"""对话状态机：驱动 IDLE -> LISTENING -> THINKING -> SPEAKING 的流转。

状态图见技术方案 §5.3。M0 只实现主链路，超时与打断留待 M2/M7。
"""

from __future__ import annotations

from enum import Enum, auto

from .eventbus import EventBus

# 所有 LLM 通道都失败时的兜底话术（技术方案 §7.5）
FALLBACK_LINE = "抱歉，我这会儿有点转不动，你再说一遍好吗？"


class State(Enum):
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()
    PROACTIVE = auto()


class DialogueFSM:
    def __init__(self, bus: EventBus, pipeline, tts) -> None:
        self.state = State.IDLE
        self.bus = bus
        self.pipeline = pipeline
        self.tts = tts

    async def to(self, state: State) -> None:
        """切换状态并广播 state.changed。"""
        if state is self.state:
            return
        self.state = state
        await self.bus.emit("state.changed", {"state": state.name})

    async def on_wake(self, _payload=None) -> None:
        """唤醒词命中（或主动服务触发）后进入倾听。"""
        if self.state is not State.IDLE:
            return
        await self.bus.emit("wake.detected")
        await self.to(State.LISTENING)

    async def greet(self, text: str) -> None:
        """主动开场白：走 PROACTIVE -> SPEAKING，复用 TTS 播放。"""
        if not text.strip() or self.state is not State.IDLE:
            return
        await self.to(State.PROACTIVE)
        await self.to(State.SPEAKING)
        await self.tts.play(text)
        await self.to(State.IDLE)

    async def on_utterance(self, text: str, _payload=None) -> None:
        """收到一句完整用户话语：思考 -> 逐句说话 -> 回到待机。"""
        if not text.strip():
            return
        await self.to(State.THINKING)
        await self.to(State.SPEAKING)
        try:
            async for sentence in self.pipeline.run(text):
                await self.tts.play(sentence)
        except Exception as exc:
            await self.bus.emit("llm.failed", {"error": f"{type(exc).__name__}: {exc}"})
            await self.tts.play(FALLBACK_LINE)
        await self.to(State.IDLE)
