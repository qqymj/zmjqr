"""屏幕表情通道（M5）。

技术方案 §4.3：1.28" 圆屏渲染眼睛/眨眼/表情包；§5.8：眨眼用帧序列、由独立
协程驱动（协程在 controller 里，驱动只负责"画一帧"）。

emotion → 一帧（眼形 + 嘴形）。无硬件时 ``ConsoleFace`` 用文字预览；
接圆屏后换成渲染真实帧序列的实现即可。
"""

from __future__ import annotations

from typing import Protocol

# 情绪 -> (眼睛, 嘴)，组成 "(眼 嘴)" 的一帧
FRAMES: dict[str, tuple[str, str]] = {
    "happy": ("^ ^", "◡"),
    "excited": ("⌒ ⌒", "▽"),
    "sad": ("· ·", "︵"),
    "anxious": ("• •", "﹏"),
    "angry": ("• •", "‾"),
    "tired": ("- -", "︶"),
    "neutral": ("• •", "‿"),
}

EMOTION_CN = {
    "happy": "开心",
    "excited": "雀跃",
    "sad": "低落",
    "anxious": "不安",
    "angry": "生气",
    "tired": "疲倦",
    "neutral": "平静",
}


def frame_for(emotion: str) -> str:
    """情绪 → 一帧文字表情；未知情绪回退 neutral，保证屏幕不空。"""
    eyes, mouth = FRAMES.get(emotion, FRAMES["neutral"])
    return f"({eyes} {mouth})"


class FaceDriver(Protocol):
    async def render(self, emotion: str) -> None: ...

    async def blink(self) -> None: ...

    async def aclose(self) -> None: ...


class ConsoleFace:
    """终端预览：表情变化时才打印（眨眼不刷屏，真实圆屏才会画眨眼帧）。"""

    def __init__(self) -> None:
        self.emotion: str | None = None

    async def render(self, emotion: str) -> None:
        if emotion == self.emotion:
            return
        self.emotion = emotion
        print(f"        [表情] {EMOTION_CN.get(emotion, emotion)} {frame_for(emotion)}")

    async def blink(self) -> None:
        """真实圆屏在这里插一帧闭眼；终端预览无可见输出。"""

    async def aclose(self) -> None:
        pass


class NullFace:
    async def render(self, emotion: str) -> None:
        pass

    async def blink(self) -> None:
        pass

    async def aclose(self) -> None:
        pass
